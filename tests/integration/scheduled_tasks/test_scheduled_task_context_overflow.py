import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import loguru
import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.background_tasks.scheduler as scheduler_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.message_platforms.notifier as notifier_module
import app.core.session.notifier as session_notifier_module
import app.core.session_reply_queue.consumer as consumer_module
import app.core.session_reply_queue.executor_agent_work as executor_agent_work_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.core.utils.context_summary.cleanup as context_summary_cleanup_module
import app.core.utils.context_summary.merge as context_summary_merge_module
import app.core.utils.context_summary.reduction as context_summary_reduction_module
import app.core.utils.context_summary.service as context_summary_service_module
import app.core.utils.context_summary.stage as context_summary_stage_module
from app.core.constants import (
    ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED,
    ERR_LLM_CONTEXT_LENGTH_CONFIG_MISMATCH,
    FILE_TOOL_NAME,
    READ_MULTIMODAL_FILE_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.exceptions import LLMContextLengthException, ServerException
from app.core.i18n import t
from app.core.prompts import SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
from app.core.utils.context_summary import ContextSummaryState, ContextSummaryTriggerMode
from app.models.channel import ModelChannel
from app.models.message import ImagePart, InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType, StoredConversationSummary
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.providers.llm.client import LLMClient, estimate_request_context_tokens
from tests.integration.scheduled_tasks.scheduled_workflow_support import (
    PROFILE_ID,
    SESSION_ID,
    UID,
    USAGE,
    _create_due_run_once_scheduled_task,
    db_session,
    session_factory,
)

__all__ = ("db_session", "session_factory")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "checkpoint_kind",
    [
        pytest.param("tool_history", id="tool-history"),
        pytest.param("existing_summary", id="existing-summary"),
    ],
)
async def test_scheduled_agent_work_context_overflow_checkpoint_resume(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_kind: str,
    tmp_path: Path,
) -> None:
    chat_model_id = "scheduled-context-chat-model"
    summary_model_id = "scheduled-context-summary-model"
    main_summary_marker = "MAIN_SESSION_SUMMARY_MUST_NOT_LEAK_7C2A"
    main_history_marker = "MAIN_FOREGROUND_HISTORY_MUST_NOT_LEAK_1D9F"
    original_block_marker = "TASK_ORIGINAL_BLOCK_MUST_SURVIVE_4A81"
    tool_result_tail_marker = "TASK_TOOL_RESULT_TAIL_96B3"
    existing_summary_marker = "TASK_EXISTING_SUMMARY_BODY_2E74"
    multimodal_tool_call_id = "scheduled-inventory-image-read-1"
    image_path = (tmp_path / "inventory.png").resolve()
    Image.new("RGB", (2, 2), color=(20, 40, 60)).save(image_path)
    multimodal_tool_result = {
        "type": "multimodal_file_read",
        "status": "success",
        "modality": "image",
        "path": str(image_path),
        "message": "库存图片已读取",
    }
    task_instruction = f"请核对每日库存并汇总异常。{original_block_marker}"
    expected_environment = {"runtime_context": "SCHEDULED_TASK_RUNTIME_CONTEXT_8F31"}
    expected_response_settings = {"markdown": False, "max_output_tokens": 321}
    expected_platform_constraints = "SCHEDULED_TASK_PLATFORM_CONSTRAINTS_5B72"
    expected_platform_guidance = "SCHEDULED_TASK_PLATFORM_GUIDANCE_3C64"
    expected_execution_guidance = SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT.strip()
    seed_environment_prompt = json.dumps(
        {
            "environment": expected_environment,
            "response_settings": expected_response_settings,
            "platform_constraints": expected_platform_constraints,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    scheduled_task.message = task_instruction

    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    configs = deepcopy(profile.configs)
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": chat_model_id, "priority": 1, "weight": 1}]
    configs["channel"]["context_summary_channel"]["rules"] = [{"channel_id": 2, "model_id": summary_model_id, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["other"]["context_summary_threshold_percent"] = 90
    profile.configs = configs

    main_history = Message(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID, role=MessageRole.USER, type=MessageType.TEXT, content=main_history_marker, is_processed=True)
    channels = ((1, "scheduled-context-chat-channel", "https://scheduled-context-chat.invalid", chat_model_id, 16), (2, "scheduled-context-summary-channel", "https://scheduled-context-summary.invalid", summary_model_id, 4))
    db_session.add_all(
        [
            profile,
            main_history,
            *[
                ModelChannel(id=cid, name=name, base_url=url, api_key=f"enc:v1:{mid}-key", model_ids=[{"model_id": mid, "usage": "CHAT", "protocol": "OPENAI", "image_understanding": mid == chat_model_id, "context_window_k": window, "max_tokens": 512, "temperature": 0, "top_p": 1}])
                for cid, name, url, mid, window in channels
            ],
        ]
    )
    await db_session.flush()
    main_session = await db_session.get(ChatSession, SESSION_ID)
    assert main_session is not None
    main_session.context_summary, main_session.context_summary_message_id = main_summary_marker, main_history.id
    main_session.context_summary_revision, main_session.context_content_revision = 7, 11
    await db_session.commit()
    main_summary_snapshot = (main_session.context_summary, main_session.context_summary_message_id, main_session.context_summary_revision, main_session.context_content_revision)

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    for module in (channel_cursor_module, dispatcher_module, scheduler_module, executor_interactive_module, executor_metadata_module, context_summary_service_module, context_summary_stage_module, context_summary_reduction_module, context_summary_merge_module, context_summary_cleanup_module):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)

    await scheduler_module.ScheduledTaskScheduler()._dispatch_one(scheduled_task.id)

    worker_id = f"scheduled-context-overflow-worker-{checkpoint_kind}"
    work_id: int
    chat_calls = 0
    chat_summaries: list[dict[str, Any]] = []
    summary_calls = 0
    summary_request_tokens: list[int] = []
    persisted_summary: dict[str, Any] | None = None

    def assert_main_context_isolated(messages: list[InternalMessage]) -> None:
        serialized = json.dumps([message.model_dump(mode="json") for message in messages], ensure_ascii=False)
        assert main_summary_marker not in serialized and main_history_marker not in serialized

    def request_tokens(messages: list[InternalMessage], model_id: str, kwargs: dict[str, Any]) -> int:
        return estimate_request_context_tokens(messages, kwargs.get("tools"), model_id=model_id, protocol=kwargs["protocol"])

    def get_summary_content(messages: list[InternalMessage]) -> dict[str, Any]:
        for message in messages:
            if message.role != MessageRole.USER:
                continue
            if message.conversation_summary is not None:
                return {
                    "conversation_summary": message.conversation_summary.content,
                    "user_message": message.content,
                }
            if not isinstance(message.content, str):
                continue
            try:
                payload = json.loads(message.content)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or "conversation_summary" not in payload or "user_message" not in payload:
                continue
            return {
                "conversation_summary": payload["conversation_summary"],
                "user_message": payload["user_message"],
            }
        raise AssertionError("compressed summary is missing from the request")

    def covered_user_content(summary: dict[str, Any]) -> str:
        content = summary["user_message"]
        assert isinstance(content, str)
        return content

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        nonlocal chat_calls, persisted_summary, summary_calls
        model_id = kwargs["model_id"]
        messages: list[InternalMessage] = kwargs["messages"]
        assert_main_context_isolated(messages)
        if model_id == summary_model_id:
            summary_calls += 1
            request_token_count = request_tokens(messages, summary_model_id, kwargs)
            summary_request_tokens.append(request_token_count)
            assert request_token_count <= 3_232
            assert multimodal_tool_call_id not in json.dumps([message.model_dump(mode="json") for message in messages], ensure_ascii=False)
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="库存核对摘要已生成"),
                model=summary_model_id,
                usage={"prompt_tokens": request_token_count, "completion_tokens": 8, "total_tokens": request_token_count + 8},
                finish_reason="stop",
            )

        assert model_id == chat_model_id
        chat_calls += 1
        if checkpoint_kind == "tool_history":
            multimodal_tool_call = next(tool_call for message in messages if message.role == MessageRole.ASSISTANT for tool_call in message.tool_calls or [] if tool_call.id == multimodal_tool_call_id)
            assert multimodal_tool_call.name == READ_MULTIMODAL_FILE_TOOL_NAME
            assert multimodal_tool_call.arguments == {"path": str(image_path)}
            multimodal_tool_result_message = next(message for message in messages if message.role == MessageRole.TOOL and message.tool_call_id == multimodal_tool_call_id)
            assert multimodal_tool_result_message.tool_execution_status == "succeeded"
            assert json.loads(multimodal_tool_result_message.content)["tool_result"] == multimodal_tool_result
            assert messages[-1].role == MessageRole.USER
            assert isinstance(messages[-1].content, list)
            assert any(isinstance(part, ImagePart) for part in messages[-1].content)
        first_user = next((message for message in messages if message.role == MessageRole.USER), None)
        assert first_user is not None
        assert isinstance(first_user.content, str)
        first_user_payload = json.loads(first_user.content)
        assert first_user_payload["environment"] == expected_environment
        assert first_user_payload["response_settings"] == expected_response_settings
        assert first_user_payload["platform_constraints"] == expected_platform_constraints
        assert first_user_payload["platform_guidance"] == expected_platform_guidance
        assert first_user_payload["execution_guidance"] == expected_execution_guidance
        request_token_count = request_tokens(messages, chat_model_id, kwargs)
        if chat_calls == 1:
            assert 3_232 < request_token_count < 13_708
            if checkpoint_kind == "tool_history":
                assert any(tool_result_tail_marker in str(message.content) for message in messages)
            else:
                assert any(existing_summary_marker in str(message.content) for message in messages)
            raise LLMContextLengthException(provider_message="scheduled task context length exceeded")

        summary = get_summary_content(messages)
        assert summary == {
            "conversation_summary": "库存核对摘要已生成",
            "user_message": task_instruction,
        }
        assert first_user_payload["conversation_summary"] == summary["conversation_summary"]
        assert first_user_payload["user_message"] == summary["user_message"]
        assert "through_message_id" not in first_user_payload
        assert "covered_user_message_id" not in first_user_payload
        assert str(trigger_message.id) not in json.dumps(
            {key: first_user_payload[key] for key in ("conversation_summary", "user_message")},
            ensure_ascii=False,
        )
        chat_summaries.append(summary)
        if chat_calls == 2:
            async with session_factory() as checkpoint_db:
                persisted_work = await checkpoint_db.get(SessionReplyWorkItem, work_id)
                assert persisted_work is not None
                assert persisted_work.status == SessionReplyWorkStatus.RUNNING
                assert persisted_work.locked_by == worker_id
                persisted_checkpoint = await session_reply_work_item_crud.get_checkpoint(checkpoint_db, work_id)
                assert isinstance(persisted_checkpoint, dict)
                persisted_messages = [InternalMessage.model_validate(item) for item in persisted_checkpoint.get("messages", [])]
                persisted_summary = get_summary_content(persisted_messages)
                assert persisted_summary == summary
            raise RuntimeError("scheduled context interruption after checkpoint save")

        assert chat_calls == 3
        assert persisted_summary == summary
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="定时库存核对完成"),
            model=chat_model_id,
            usage=USAGE,
            finish_reason="stop",
        )

    monkeypatch.setattr(LLMClient, "generate", mock_generate)

    with pytest.raises(ServerException) as interruption:
        async with session_factory() as claim_db:
            queued_work = (await claim_db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN, SessionReplyWorkItem.session_id == SESSION_ID))).scalars().one()
            claimed_work = await session_reply_work_item_crud.claim_next(claim_db, worker_id=worker_id, lease_seconds=300)
            assert claimed_work is not None
            assert claimed_work.id == queued_work.id
            work_id = claimed_work.id

            execution_state = claimed_work.execution_state or {}
            trigger_id = execution_state["agent_work"]["instruction_message_id"]
            trigger_message = await claim_db.get(Message, trigger_id)
            assert trigger_message is not None
            assert (trigger_message.type, trigger_message.content) == (MessageType.SCHEDULED_TASK_TRIGGER, task_instruction)

            system_message = InternalMessage(role=MessageRole.SYSTEM, content="定时库存核对任务的执行上下文。")
            if checkpoint_kind == "tool_history":
                tool_call_id = "scheduled-inventory-read-1"
                checkpoint_messages = [
                    system_message,
                    InternalMessage(
                        id=trigger_message.id,
                        role=MessageRole.USER,
                        content=task_instruction,
                        environment_prompt=seed_environment_prompt,
                        guidance_prompt=expected_platform_guidance,
                        execution_guidance=expected_execution_guidance,
                    ),
                    InternalMessage(role=MessageRole.ASSISTANT, content="正在读取每日库存条目。", tool_calls=[InternalToolCall(id=tool_call_id, name=FILE_TOOL_NAME, arguments={"operation": "read", "path": "inventory/entries.txt"})]),
                    InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call_id, content=("entry " * 6500) + tool_result_tail_marker, tool_execution_status="succeeded"),
                    InternalMessage(role=MessageRole.ASSISTANT, content="正在读取库存图片。", tool_calls=[InternalToolCall(id=multimodal_tool_call_id, name=READ_MULTIMODAL_FILE_TOOL_NAME, arguments={"path": str(image_path)})]),
                    InternalMessage(role=MessageRole.TOOL, tool_call_id=multimodal_tool_call_id, content=json.dumps(multimodal_tool_result, ensure_ascii=False), tool_execution_status="succeeded"),
                ]
            else:
                existing_summary = StoredConversationSummary(
                    conversation_summary=existing_summary_marker + " " + ("entry " * 6500),
                    user_message=task_instruction,
                    covered_user_message_id=trigger_message.id,
                ).model_dump_json(exclude_none=True)
                assert existing_summary is not None
                summary_message = ContextSummaryState(
                    content=existing_summary,
                    message_id=trigger_message.id,
                    covered_user_request=InternalMessage(
                        role=MessageRole.USER,
                        content=task_instruction,
                        environment_prompt=seed_environment_prompt,
                        guidance_prompt=expected_platform_guidance,
                        execution_guidance=expected_execution_guidance,
                    ),
                ).as_message()
                assert summary_message is not None
                checkpoint_messages = [system_message, summary_message]

            checkpoint = {
                "messages": [message.model_dump(mode="json", exclude_none=True) for message in checkpoint_messages],
                "turn_messages": [],
                "reload_history": False,
                "current_turn": 1,
                "context_summary_trigger_mode": ContextSummaryTriggerMode.USER_MESSAGE.value,
                "context_summary_fixed_upper_message_id": trigger_message.id,
            }
            claimed_work.execution_state = dict(execution_state)
            claim_db.add(claimed_work)
            await claim_db.flush()
            await session_reply_work_item_crud.save_checkpoint(claim_db, work_id, checkpoint)
            await claim_db.commit()
            claim_db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, worker_id)

            await executor_agent_work_module.execute_agent_work(claim_db, claimed_work, worker_id)

    assert interruption.value.cause is not None
    assert "scheduled context interruption" in interruption.value.cause
    assert summary_calls > 1

    async with session_factory() as resume_db:
        resumed_work = await resume_db.get(SessionReplyWorkItem, work_id)
        assert resumed_work is not None
        assert resumed_work.status == SessionReplyWorkStatus.RUNNING
        assert resumed_work.locked_by == worker_id
        resume_db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, worker_id)
        response = await executor_agent_work_module.execute_agent_work(resume_db, resumed_work, worker_id)
        await resume_db.commit()

    assert response["content"] == "定时库存核对完成"
    assert chat_calls == 3
    assert len(summary_request_tokens) == summary_calls
    assert persisted_summary is not None
    assert chat_summaries == [persisted_summary, persisted_summary]
    assert persisted_summary == {
        "conversation_summary": "库存核对摘要已生成",
        "user_message": task_instruction,
    }
    assert covered_user_content(persisted_summary) == task_instruction

    async with session_factory() as verification_db:
        persisted_work = await verification_db.get(SessionReplyWorkItem, work_id)
        persisted_session = await verification_db.get(ChatSession, SESSION_ID)
        assert persisted_work is not None
        persisted_checkpoint = await session_reply_work_item_crud.get_checkpoint(verification_db, work_id)
        assert isinstance(persisted_checkpoint, dict)
        persisted_final_summary = get_summary_content([InternalMessage.model_validate(item) for item in persisted_checkpoint.get("messages", [])])

    assert persisted_final_summary == persisted_summary
    assert covered_user_content(persisted_final_summary) == task_instruction
    assert persisted_session is not None
    assert (persisted_session.context_summary, persisted_session.context_summary_message_id, persisted_session.context_summary_revision, persisted_session.context_content_revision) == main_summary_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fallback_mode",
    [pytest.param("none", id="no-fallback"), pytest.param("overflow", id="fallback-overflow"), pytest.param("success", id="fallback-success")],
)
async def test_scheduled_task_context_overflow_routes_fallback_and_persists_notification(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog,
    fallback_mode: str,
) -> None:
    primary_model_id = "scheduled-overflow-primary-model"
    fallback_model_id = "scheduled-overflow-fallback-model"
    scheduled_task = await _create_due_run_once_scheduled_task(db_session)
    assert scheduled_task.id is not None

    profile = await db_session.get(Profile, PROFILE_ID)
    assert profile is not None
    chat_models: list[tuple[int, str, int]] = [(1, primary_model_id, 1)]
    if fallback_mode != "none":
        chat_models.append((2, fallback_model_id, 2))
    configs = deepcopy(profile.configs)
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": channel_id, "model_id": model_id, "priority": priority, "weight": 1} for channel_id, model_id, priority in chat_models]
    configs["channel"]["context_summary_channel"]["rules"] = []
    configs["memory"]["precheck_enabled"] = False
    profile.configs = configs
    db_session.add_all(
        [profile]
        + [
            ModelChannel(
                id=channel_id,
                name=f"scheduled-overflow-channel-{channel_id}",
                base_url="https://llm.invalid",
                api_key=f"enc:v1:{model_id}-key",
                model_ids=[
                    {
                        "model_id": model_id,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 16,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            )
            for channel_id, model_id, _priority in chat_models
        ]
    )
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    for module in (
        channel_cursor_module,
        consumer_module,
        dispatcher_module,
        scheduler_module,
        executor_interactive_module,
        executor_lifecycle_module,
        executor_metadata_module,
        context_summary_service_module,
        context_summary_stage_module,
        context_summary_merge_module,
        context_summary_reduction_module,
        context_summary_cleanup_module,
        notifier_module,
        session_notifier_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)

    attempted_models: list[str] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        model_id = kwargs["model_id"]
        attempted_models.append(model_id)
        if model_id == primary_model_id:
            raise LLMContextLengthException(provider_message="primary context length exceeded")
        assert model_id == fallback_model_id and fallback_mode != "none"
        if fallback_mode == "overflow":
            raise LLMContextLengthException(provider_message="fallback context length exceeded")
        return InternalResponse(message=InternalMessage(role=MessageRole.ASSISTANT, content="备用渠道完成"), model=fallback_model_id, usage=USAGE, finish_reason="stop")

    monkeypatch.setattr(LLMClient, "generate", generate)

    await scheduler_module.ScheduledTaskScheduler()._dispatch_one(scheduled_task.id)

    worker_id = "scheduled-overflow-consumer-worker"
    async with session_factory() as claim_db:
        claimed_work = await session_reply_work_item_crud.claim_next(claim_db, worker_id=worker_id, lease_seconds=300)
        assert claimed_work is not None
        assert claimed_work.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN
        assert claimed_work.work_type == SessionReplyWorkType.AGENT_WORK
        assert claimed_work.id is not None
        work_id = claimed_work.id
        attempt_count = claimed_work.attempt_count
        max_attempts = claimed_work.max_attempts

        execution_state = claimed_work.execution_state or {}
        trigger_id = execution_state["agent_work"]["instruction_message_id"]
        trigger_message = await claim_db.get(Message, trigger_id)
        assert trigger_message is not None and trigger_message.id == trigger_id
        existing_summary = StoredConversationSummary(
            conversation_summary="已有简短摘要",
            user_message=trigger_message.content,
            covered_user_message_id=trigger_message.id,
        ).model_dump_json(exclude_none=True)
        assert existing_summary is not None
        summary_message = ContextSummaryState(content=existing_summary, message_id=trigger_message.id).as_message()
        assert summary_message is not None
        checkpoint_messages = [
            InternalMessage(role=MessageRole.SYSTEM, content="定时任务执行上下文。"),
            summary_message,
        ]
        checkpoint = {
            "messages": [message.model_dump(mode="json", exclude_none=True) for message in checkpoint_messages],
            "turn_messages": [],
            "reload_history": False,
            "current_turn": 1,
            "context_summary_trigger_mode": ContextSummaryTriggerMode.USER_MESSAGE.value,
            "context_summary_fixed_upper_message_id": trigger_message.id,
        }
        claimed_work.execution_state = dict(execution_state)
        claim_db.add(claimed_work)
        await claim_db.flush()
        await session_reply_work_item_crud.save_checkpoint(claim_db, work_id, checkpoint)
        await claim_db.commit()

    expected_summary_warning = t(
        "LOG_CONTEXT_SUMMARY_CHANNEL_FAILED",
        locale="zh",
        error=t(ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED, locale="zh"),
    )
    handler_id = loguru.logger.add(caplog.handler, format="{message}", level="WARNING")
    try:
        await consumer_module.SessionReplyConsumer()._run_claimed(work_id, worker_id, attempt_count, max_attempts, "zh")
    finally:
        loguru.logger.remove(handler_id)
    assert expected_summary_warning in caplog.text

    async with session_factory() as verification_db:
        persisted_work = await verification_db.get(SessionReplyWorkItem, work_id)
        assert persisted_work is not None
        assert persisted_work.result_message_id is not None
        persisted_messages = list((await verification_db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id.asc()))).scalars().all())
        session_events = list((await verification_db.execute(select(SessionEvent).where(SessionEvent.session_id == SESSION_ID, SessionEvent.uid == UID).order_by(SessionEvent.id.asc()))).scalars().all())

    result_message = next(message for message in persisted_messages if message.id == persisted_work.result_message_id)
    trigger_messages = [message for message in persisted_messages if message.type == MessageType.SCHEDULED_TASK_TRIGGER]
    error_messages = [message for message in persisted_messages if message.role == MessageRole.ERR]
    assistant_messages = [message for message in persisted_messages if message.role == MessageRole.ASSISTANT]
    assert len(trigger_messages) == 1
    trigger_message = trigger_messages[0]
    assert trigger_message.role == MessageRole.USER
    assert trigger_message.work_id == work_id
    assert persisted_work.attempt_count == 1
    assert persisted_work.event_sent is True
    assert result_message.work_id is None
    assert result_message.type == MessageType.TEXT
    assert attempted_models == ([primary_model_id] if fallback_mode == "none" else [primary_model_id, fallback_model_id])
    assert len(session_events) == 1
    event = session_events[0].event
    assert event["source"] == "scheduled_task"
    assert event["work_id"] == work_id
    assert event["trigger_message_id"] == trigger_message.id
    assert event["message_id"] == result_message.id
    assert event["content"] == result_message.content

    if fallback_mode == "success":
        assert persisted_work.status == SessionReplyWorkStatus.SUCCEEDED
        assert result_message.role == MessageRole.ASSISTANT
        assert result_message.content == "备用渠道完成"
        assert error_messages == []
        assert assistant_messages == [result_message]
        assert event["type"] == "proactive_reply"
    else:
        assert persisted_work.status == SessionReplyWorkStatus.FAILED
        assert persisted_work.error == t(ERR_LLM_CONTEXT_LENGTH_CONFIG_MISMATCH, locale="zh")
        assert result_message.role == MessageRole.ERR
        assert result_message.content == t(ERR_CONTEXT_SUMMARY_COMPRESSION_FAILED, locale="zh")
        assert error_messages == [result_message]
        assert assistant_messages == []
        assert event["type"] == "proactive_reply_error"
