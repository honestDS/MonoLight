import json
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.background_tasks.external_work as external_work_module
import app.core.background_tasks.result_delivery as result_delivery_module
import app.core.background_tasks.scheduler as scheduler_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.dispatchers.interactive_generation as interactive_generation_module
import app.core.message_platforms.notifier as notifier_module
import app.core.session.notifier as session_notifier_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.core.tools.send_file_to_user as send_file_to_user_module
from app.core.background_tasks.external_work import resume_external_work_for_task
from app.core.constants import (
    END_SESSION_SCOPE_TASK,
    END_SESSION_TOOL_NAME,
    IMAGE_GENERATION_TOOL_NAME,
    MANAGE_SCHEDULED_TASKS_TOOL_NAME,
    MANAGE_TODO_TOOL_NAME,
    SEND_FILE_TO_USER_TOOL_NAME,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.prompts import SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT
from app.core.utils.time import get_local_time
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.background_task import BackgroundTask, BackgroundTaskReplyMode
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from app.providers.llm.client import LLMClient
from tests.integration.scheduled_tasks.scheduled_workflow_support import (
    MODEL_ID,
    PROFILE_ID,
    UID,
    USAGE,
    db_session,
    session_factory,
)

__all__ = ("db_session", "session_factory")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "saved_execution_state",
    [
        {},
        {
            "stream_requested": True,
            "show_tool_calls": True,
            "expose_tool_call_content": True,
            "context_summary_events_requested": True,
        },
    ],
    ids=["default-state", "saved-output-flags"],
)
@pytest.mark.parametrize(
    "scheduled_message",
    [
        pytest.param("请生成报告并发送给用户", id="original"),
        pytest.param(" \t请生成报告并发送给用户", id="leading-whitespace"),
        pytest.param("请生成报告并发送给用户\n", id="trailing-newline"),
        pytest.param("\t \n请生成报告并发送给用户 \t\r\n", id="surrounding-whitespace"),
    ],
)
async def test_scheduled_task_execution_supports_multiround_and_send_file(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    saved_execution_state: dict[str, Any],
    scheduled_message: str,
) -> None:
    report_file = tmp_path / "scheduled_report.txt"
    report_file.write_text("scheduled task report content", encoding="utf-8")

    session_id = "scheduled-multiround-session"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["tool"]["enabled_tools"] = [SEND_FILE_TO_USER_TOOL_NAME, MANAGE_SCHEDULED_TASKS_TOOL_NAME]
    configs["tool"]["allowed_operation_dirs"] = [str(tmp_path)]

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-task-multiround-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    }
                ],
            ),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-task-multiround-profile", configs=configs, is_default=True),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=PROFILE_ID, goal_mode=True, max_turns=5))
    await db_session.commit()

    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_interactive_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(send_file_to_user_module, "FILE_TOKEN_DIR", tmp_path / "file_download_tokens")
    monkeypatch.setattr(send_file_to_user_module, "_get_encryption_key", lambda: b"g" * 32)

    scheduled_task = ScheduledTask(
        name="Scheduled report task",
        uid=UID,
        session_id=session_id,
        message=scheduled_message,
        interval_seconds=60,
        run_once=True,
        next_run_at=get_local_time() - timedelta(seconds=1),
    )
    db_session.add(scheduled_task)
    await db_session.commit()
    await db_session.refresh(scheduled_task)
    assert scheduled_task.id is not None

    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(scheduled_task.id)

    work_item = (
        (
            await db_session.execute(
                select(SessionReplyWorkItem).where(
                    SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
                    SessionReplyWorkItem.session_id == session_id,
                )
            )
        )
        .scalars()
        .one()
    )

    worker_id = "worker-multiround"
    work_item.status = SessionReplyWorkStatus.RUNNING
    work_item.locked_by = worker_id
    work_item.lock_until = int(get_local_time().timestamp()) + 300
    await db_session.commit()
    await db_session.refresh(work_item)
    db_session.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_item.id, worker_id)

    work_item.execution_state = {**(work_item.execution_state or {}), **saved_execution_state}
    db_session.add(work_item)
    await db_session.commit()
    await db_session.refresh(work_item)

    calls: list[dict[str, Any]] = []

    async def mock_create_chat_completion(**kwargs: Any) -> InternalResponse:
        calls.append(kwargs)
        turn = len(calls)
        if turn == 1:
            user_messages = [message for message in kwargs["messages"] if message.role == MessageRole.USER]
            assert len(user_messages) == 1
            payload = json.loads(user_messages[0].content)
            assert payload["user_message"] == scheduled_message.strip()
            assert payload["execution_guidance"] == SCHEDULED_TASK_TRIGGER_EXECUTION_GUIDANCE_PROMPT.strip()
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="内部工具中间正文",
                    tool_calls=[
                        InternalToolCall(
                            id="call-send-file-1",
                            name=SEND_FILE_TO_USER_TOOL_NAME,
                            arguments={"files": [{"path": str(report_file.resolve())}]},
                        )
                    ],
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if turn == 2:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="call-end-session-2",
                            name=END_SESSION_TOOL_NAME,
                            arguments={"skip_reply": False},
                        )
                    ],
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if turn == 3:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="报告已生成并发送完成",
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        raise AssertionError(f"Unexpected turn {turn}")

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_create_chat_completion)
    monkeypatch.setattr(
        interactive_generation_module.LLMClient,
        "create_chat_completion",
        mock_create_chat_completion,
        raising=False,
    )
    monkeypatch.setattr(LLMClient, "generate", mock_create_chat_completion)
    monkeypatch.setattr(LLMClient, "create_chat_completion", mock_create_chat_completion, raising=False)

    monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(session_notifier_module, "AsyncSessionLocal", session_factory)

    assert work_item.id is not None
    original_send_session_event = executor_lifecycle_module.send_session_event
    send_attempts = 0

    async def fail_first_send(uid: str, session_id: str, event: dict[str, Any]) -> None:
        nonlocal send_attempts
        send_attempts += 1
        if send_attempts == 1:
            raise RuntimeError("simulated notification failure")
        await original_send_session_event(uid, session_id, event)

    monkeypatch.setattr(executor_lifecycle_module, "send_session_event", fail_first_send)

    with pytest.raises(RuntimeError, match="simulated notification failure"):
        await executor_lifecycle_module.execute_session_reply_work(work_item.id, worker_id)

    async with session_factory() as failed_delivery_db:
        failed_work = await failed_delivery_db.get(SessionReplyWorkItem, work_item.id)
        assert failed_work is not None
        assert failed_work.status == SessionReplyWorkStatus.RUNNING
        assert failed_work.event_sent is False
        assert failed_work.result_message_id is not None
        failed_result = await failed_delivery_db.get(Message, failed_work.result_message_id)
        assert failed_result is not None
        assert failed_result.work_id is None
        failed_session = await failed_delivery_db.get(ChatSession, session_id)
        assert failed_session is not None
        assert failed_session.latest_message_id == failed_result.id
        failed_model_history = await message_crud.get_history_backward_by_id(failed_delivery_db, session_id=session_id, uid=UID)
        failed_ui_history = await message_crud.get_history_paged(failed_delivery_db, session_id=session_id, uid=UID)
        assert [message.id for message in reversed(failed_model_history)] == [failed_result.id]
        assert [message.id for message in reversed(failed_ui_history)] == [failed_result.id]
        assert list((await failed_delivery_db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID))).scalars().all()) == []

    retry_worker_id = "worker-multiround-retry"
    async with session_factory() as retry_db:
        assert await session_reply_work_item_crud.release_for_retry(
            retry_db,
            work_id=work_item.id,
            worker_id=worker_id,
            error="simulated notification failure",
            delay_seconds=0,
        )
        reclaimed = await session_reply_work_item_crud.claim_next(retry_db, worker_id=retry_worker_id, lease_seconds=300)
        assert reclaimed is not None
        assert reclaimed.id == work_item.id

    await executor_lifecycle_module.execute_session_reply_work(work_item.id, retry_worker_id)

    async with session_factory() as verification_db:
        persisted_messages = list((await verification_db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))).scalars().all())
        persisted_work = await verification_db.get(SessionReplyWorkItem, work_item.id)
        session_events = list((await verification_db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID).order_by(SessionEvent.id.asc()))).scalars().all())
        session = await verification_db.get(ChatSession, session_id)
        model_history = await message_crud.get_history_backward_by_id(verification_db, session_id=session_id, uid=UID)
        ui_history = await message_crud.get_history_paged(verification_db, session_id=session_id, uid=UID)

    assert persisted_work is not None
    assert persisted_work.status == SessionReplyWorkStatus.SUCCEEDED
    assert persisted_work.result_message_id is not None
    saved_response = (persisted_work.execution_state or {}).get("response")
    assert isinstance(saved_response, dict)
    assert len(saved_response["files"]) == 1
    sent_file_info = saved_response["files"][0]
    assert sent_file_info["name"] == report_file.name
    assert "id" in sent_file_info

    final_message = next(message for message in persisted_messages if message.id == persisted_work.result_message_id)
    assert final_message.role == MessageRole.ASSISTANT
    assert final_message.type == MessageType.TEXT
    assert final_message.work_id is None
    parsed_content = json.loads(final_message.content)
    assert parsed_content["type"] == "assistant_files"
    assert parsed_content["text"] == "报告已生成并发送完成"
    assert len(parsed_content["files"]) == 1
    assert parsed_content["files"][0]["id"] == sent_file_info["id"]

    assert len(calls) == 3
    assert send_attempts == 2
    assert session is not None
    assert session.latest_message_id == final_message.id
    assert [message.content for message in reversed(model_history)] == [final_message.content]
    assert [message.content for message in reversed(ui_history)] == [final_message.content]

    trigger_messages = [message for message in persisted_messages if message.type == MessageType.SCHEDULED_TASK_TRIGGER]
    assert len(trigger_messages) == 1
    assert trigger_messages[0].content == scheduled_message
    assert trigger_messages[0].work_id == work_item.id
    tool_messages = [message for message in persisted_messages if message.type in {MessageType.TOOL_CALL, MessageType.TOOL_RESULT}]
    assert tool_messages
    assert all(message.work_id == work_item.id for message in tool_messages)
    control_messages = [msg for msg in persisted_messages if isinstance(msg.provider_metadata, dict) and msg.provider_metadata.get("internal_control") == END_SESSION_TOOL_NAME]
    assert len(control_messages) == 2
    assert all(msg.provider_metadata.get("execution_scope") == END_SESSION_SCOPE_TASK for msg in control_messages)

    assert len(session_events) == 1
    final_event = session_events[0].event
    assert final_event["type"] == "proactive_reply"
    assert final_event["work_id"] == work_item.id
    assert final_event["content"] == "报告已生成并发送完成"
    assert final_event["files"] == saved_response["files"]
    assert final_event["message_id"] == final_message.id
    assert final_event["history"] == []
    assert "内部工具中间正文" not in json.dumps(final_event, ensure_ascii=False)
    assert all(event.event.get("type") != "tool_results_update" for event in session_events)


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_profile", [False, True])
async def test_scheduled_task_background_tool_resumes_same_work_with_task_scope(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    switch_profile: bool,
) -> None:
    session_id = "scheduled-background-resume-session"
    switched_profile_id = PROFILE_ID + 1
    switched_model_id = "scheduled-background-switched-model"
    configs = ProfileConfig.model_validate({}).model_dump()
    configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]
    configs["memory"]["precheck_enabled"] = False
    configs["channel"]["image_generation_channel"] = {"rules": [{"channel_id": 1, "model_id": "scheduled-image-model", "priority": 1, "weight": 1}]}
    configs["tool"]["enabled_tools"] = [IMAGE_GENERATION_TOOL_NAME, MANAGE_SCHEDULED_TASKS_TOOL_NAME, MANAGE_TODO_TOOL_NAME]

    db_session.add_all(
        [
            User(uid=UID, username=UID),
            ModelChannel(
                id=1,
                name="scheduled-background-resume-channel",
                base_url="https://llm.invalid",
                api_key="enc:v1:stored-test-key",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 512,
                        "temperature": 0,
                        "top_p": 1,
                    },
                    {
                        "model_id": "scheduled-image-model",
                        "usage": "IMAGE_GENERATION",
                        "protocol": "OPENAI_IMAGE",
                        "size": "1024x1024",
                        "quality": "auto",
                    },
                ],
            ),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-background-resume-profile", configs=configs, is_default=True),
        ]
    )
    await db_session.flush()
    db_session.add(ChatSession(session_id=session_id, uid=UID, profile_id=PROFILE_ID, goal_mode=False, max_turns=5))
    await db_session.flush()
    scheduled_task = ScheduledTask(
        name="Scheduled background resume",
        uid=UID,
        session_id=session_id,
        message="run background shell and report",
        interval_seconds=60,
        run_once=True,
        next_run_at=get_local_time() - timedelta(seconds=1),
    )
    db_session.add(scheduled_task)
    await db_session.commit()
    await db_session.refresh(scheduled_task)
    assert scheduled_task.id is not None

    for module in (
        channel_cursor_module,
        dispatcher_module,
        scheduler_module,
        executor_interactive_module,
        executor_lifecycle_module,
        executor_metadata_module,
        external_work_module,
        notifier_module,
        session_notifier_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")

    calls: list[dict[str, Any]] = []

    async def mock_generate(**kwargs: Any) -> InternalResponse:
        calls.append(kwargs)
        assert kwargs["model_id"] == MODEL_ID
        if len(calls) == 1:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="scheduled-background-image",
                            name=IMAGE_GENERATION_TOOL_NAME,
                            arguments={"prompt": "scheduled background image"},
                        )
                    ],
                ),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="tool_calls",
            )
        if len(calls) == 2:
            resumed_messages: list[InternalMessage] = kwargs["messages"]
            assert any(message.tool_calls and any(tool_call.id == "scheduled-background-image" for tool_call in message.tool_calls) for message in resumed_messages)
            assert any("image-1" in str(message.content) for message in resumed_messages if message.content is not None)
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="scheduled background completed"),
                model=MODEL_ID,
                usage=USAGE,
                finish_reason="stop",
            )
        raise AssertionError(f"Unexpected model call {len(calls)}")

    monkeypatch.setattr(interactive_generation_module.LLMClient, "generate", mock_generate)
    monkeypatch.setattr(LLMClient, "generate", mock_generate)

    scheduler = scheduler_module.ScheduledTaskScheduler()
    await scheduler._dispatch_one(scheduled_task.id)

    async with session_factory() as db:
        work = (
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == session_id,
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                    )
                )
            )
            .scalars()
            .one()
        )
        assert work.id is not None
        work_id = work.id
        claimed = await session_reply_work_item_crud.claim_next(db, worker_id="scheduled-background-worker-1", lease_seconds=300)
        assert claimed is not None and claimed.id == work_id

    await executor_lifecycle_module.execute_session_reply_work(work_id, "scheduled-background-worker-1")

    async with session_factory() as db:
        waiting = await db.get(SessionReplyWorkItem, work_id)
        assert waiting is not None
        assert waiting.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
        assert waiting.profile_id == PROFILE_ID
        works = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id))).scalars().all())
        assert [item.id for item in works] == [work_id]
        assert await message_crud.get_history_backward_by_id(db, session_id=session_id, uid=UID) == []
        assert await message_crud.get_history_paged(db, session_id=session_id, uid=UID) == []
        task = (await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == session_id))).scalars().one()
        assert task.id is not None
        assert task.auto_reply is False
        assert task.extra["reply_mode"] == BackgroundTaskReplyMode.RESUME_ORIGIN.value
        task_id = task.id
        claimed_task = await background_task_crud.try_claim(db, task_id=task_id, worker_id="scheduled-background-task-worker", lease_seconds=300)
        assert claimed_task is not None
        assert await background_task_crud.mark_succeeded(
            db,
            task_id=task_id,
            worker_id="scheduled-background-task-worker",
            result={"status": "succeeded", "content": {"files": [{"id": "image-1", "name": "scheduled.png"}]}},
            auto_reply=False,
        )

    if switch_profile:
        switched_configs = deepcopy(configs)
        switched_configs["channel"]["chat_channel"]["rules"] = [{"channel_id": 1, "model_id": switched_model_id, "priority": 1, "weight": 1}]
        async with session_factory() as db:
            channel = await db.get(ModelChannel, 1)
            assert channel is not None
            switched_model = deepcopy(next(model for model in channel.model_ids if model["model_id"] == MODEL_ID and model["usage"] == "CHAT"))
            switched_model["model_id"] = switched_model_id
            channel.model_ids = [*channel.model_ids, switched_model]
            db.add(
                Profile(
                    id=switched_profile_id,
                    uid=UID,
                    name="scheduled-background-switched-profile",
                    configs=switched_configs,
                )
            )
            await db.flush()
            session = await db.get(ChatSession, session_id)
            assert session is not None
            session.profile_id = switched_profile_id
            session.profile_override_id = switched_profile_id
            await db.commit()
        async with session_factory() as db:
            switched_work = await db.get(SessionReplyWorkItem, work_id)
            switched_session = await db.get(ChatSession, session_id)
            switched_profile = await db.get(Profile, switched_profile_id)
            assert switched_work is not None
            assert switched_work.profile_id == PROFILE_ID
            assert switched_session is not None
            assert (switched_session.profile_id, switched_session.profile_override_id) == (switched_profile_id, switched_profile_id)
            assert switched_profile is not None

    async with session_factory() as db:
        foreground_work, _created = await session_reply_work_item_crud.enqueue(
            db,
            uid=UID,
            session_id=session_id,
            profile_id=PROFILE_ID,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id="temporary-foreground-input",
            dedupe_key="foreground-message:scheduled-background-isolation",
            commit=False,
        )
        await db.flush()
        await db.refresh(foreground_work)
        assert foreground_work.id is not None
        foreground_worker_id = "scheduled-foreground-isolation-worker"
        claimed_foreground = await session_reply_work_item_crud.claim_next(db, worker_id=foreground_worker_id, lease_seconds=300)
        assert claimed_foreground is not None
        assert claimed_foreground.id == foreground_work.id
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (foreground_work.id, foreground_worker_id)
        assert await result_delivery_module.receive_pending_external_results(db, uid=UID, session_id=session_id, profile_id=PROFILE_ID) == []
        task = await db.get(BackgroundTask, task_id)
        assert task is not None
        reply_owner = task.extra.get("reply_owner") if isinstance(task.extra, dict) else None
        assert not (isinstance(reply_owner, dict) and reply_owner.get("work_id") == foreground_work.id)
        assert await session_reply_work_item_crud.cancel_session(db, session_id=session_id, uid=UID, interactive_only=True) == 1
        cancelled_foreground = await db.get(SessionReplyWorkItem, foreground_work.id)
        assert cancelled_foreground is not None
        assert cancelled_foreground.status == SessionReplyWorkStatus.CANCELLED

    assert await resume_external_work_for_task(task_id) is True

    async with session_factory() as db:
        resumed = await db.get(SessionReplyWorkItem, work_id)
        assert resumed is not None
        assert resumed.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert await message_crud.get_history_backward_by_id(db, session_id=session_id, uid=UID) == []
        assert await message_crud.get_history_paged(db, session_id=session_id, uid=UID) == []
        resumed_events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID))).scalars().all())
        assert all(event.event.get("type") != "tool_results_update" for event in resumed_events)
        claimed = await session_reply_work_item_crud.claim_next(db, worker_id="scheduled-background-worker-2", lease_seconds=300)
        assert claimed is not None and claimed.id == work_id

    await executor_lifecycle_module.execute_session_reply_work(work_id, "scheduled-background-worker-2")

    async with session_factory() as db:
        finished = await db.get(SessionReplyWorkItem, work_id)
        assert finished is not None
        assert finished.status == SessionReplyWorkStatus.SUCCEEDED
        assert finished.profile_id == PROFILE_ID
        agent_works = list(
            (
                await db.execute(
                    select(SessionReplyWorkItem).where(
                        SessionReplyWorkItem.session_id == session_id,
                        SessionReplyWorkItem.work_type == SessionReplyWorkType.AGENT_WORK,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [item.id for item in agent_works] == [work_id]
        result_message = await db.get(Message, finished.result_message_id)
        assert result_message is not None
        assert result_message.profile_id == PROFILE_ID
        assert result_message.content == "scheduled background completed"
        assert result_message.work_id is None
        session = await db.get(ChatSession, session_id)
        assert session is not None
        if switch_profile:
            assert (session.profile_id, session.profile_override_id) == (switched_profile_id, switched_profile_id)
        assert session.latest_message_id == result_message.id
        model_history = await message_crud.get_history_backward_by_id(db, session_id=session_id, uid=UID)
        ui_history = await message_crud.get_history_paged(db, session_id=session_id, uid=UID)
        assert [message.content for message in reversed(model_history)] == [result_message.content]
        assert [message.content for message in reversed(ui_history)] == [result_message.content]
        session_events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID).order_by(SessionEvent.id.asc()))).scalars().all())

    assert [event.event["type"] for event in session_events] == ["work_ready", "proactive_reply"]
    assert all(event.event.get("type") != "tool_results_update" for event in session_events)
    final_event = session_events[-1].event
    assert final_event["work_id"] == work_id
    assert final_event["content"] == "scheduled background completed"
    assert final_event["files"] == []
    assert final_event["history"] == []

    assert len(calls) == 2
    for call in calls:
        tool_names = {tool["function"]["name"] for tool in call["tools"]}
        assert IMAGE_GENERATION_TOOL_NAME in tool_names
        assert MANAGE_SCHEDULED_TASKS_TOOL_NAME not in tool_names
        assert MANAGE_TODO_TOOL_NAME not in tool_names


@pytest.mark.asyncio
async def test_scheduled_agent_work_skip_reply_completes_silently_without_affecting_foreground(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "scheduled-agent-skip-reply-session"
    db_session.add_all(
        [
            User(uid=UID, username=UID),
            Profile(id=PROFILE_ID, uid=UID, name="scheduled-agent-skip-reply-profile", configs=ProfileConfig.model_validate({}).model_dump()),
            ChatSession(session_id=session_id, uid=UID, profile_id=PROFILE_ID),
        ]
    )
    await db_session.commit()

    agent_work, _created = await scheduler_module.session_reply_queue_manager.enqueue_agent_work(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
        source_id="skip-reply-trigger",
        dedupe_key="scheduled-agent-work:skip-reply",
        spec=AgentWorkSpec(
            instruction="无需回复的计划任务",
            execution_scope=END_SESSION_SCOPE_TASK,
            report_target=AgentWorkReportTarget.SESSION,
        ),
        commit=False,
    )
    await db_session.commit()
    await db_session.refresh(agent_work)
    assert agent_work.id is not None

    worker_id = "scheduled-agent-skip-reply-worker"
    claimed_agent_work = await session_reply_work_item_crud.claim_next(db_session, worker_id=worker_id, lease_seconds=300)
    assert claimed_agent_work is not None
    assert claimed_agent_work.id == agent_work.id
    claimed_agent_work.execution_state = {
        **(claimed_agent_work.execution_state or {}),
        "response": {
            "skip_reply": True,
            "choices": [],
            "history": [],
            "files": None,
        },
    }
    db_session.add(claimed_agent_work)

    foreground_work, _created = await session_reply_work_item_crud.enqueue(
        db_session,
        uid=UID,
        session_id=session_id,
        profile_id=PROFILE_ID,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="parallel-foreground-input",
        dedupe_key="foreground-message:parallel-skip-reply",
        commit=False,
    )
    await db_session.commit()
    await db_session.refresh(foreground_work)
    assert foreground_work.id is not None

    for module in (executor_lifecycle_module, notifier_module, session_notifier_module):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)

    await executor_lifecycle_module.execute_session_reply_work(agent_work.id, worker_id)

    async with session_factory() as verification_db:
        finished = await verification_db.get(SessionReplyWorkItem, agent_work.id)
        assert finished is not None
        assert finished.status == SessionReplyWorkStatus.SUCCEEDED
        assert finished.result_message_id is None
        session = await verification_db.get(ChatSession, session_id)
        assert session is not None
        assert session.latest_message_id is None
        model_history = await message_crud.get_history_backward_by_id(verification_db, session_id=session_id, uid=UID)
        ui_history = await message_crud.get_history_paged(verification_db, session_id=session_id, uid=UID)
        assert model_history == []
        assert ui_history == []
        session_events = list((await verification_db.execute(select(SessionEvent).where(SessionEvent.session_id == session_id, SessionEvent.uid == UID))).scalars().all())
        assert session_events == []
        foreground_after = await verification_db.get(SessionReplyWorkItem, foreground_work.id)
        assert foreground_after is not None
        assert foreground_after.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert foreground_after.result_message_id is None
