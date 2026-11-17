import copy
import json

import pytest
from loguru import logger as loguru_logger
from sqlmodel import select

import app.core.background_tasks.external_work as external_work_module
import app.core.background_tasks.reply_trigger as reply_trigger_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.background as background_dispatcher_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.message_platforms.notifier as notifier_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.providers.database as database_provider
from app.core.background_tasks.external_work import resume_external_work_for_task
from app.core.background_tasks.terminal_watch_coordinator import complete_terminal_watch_round
from app.core.constants import MANAGE_TODO_TOOL_NAME, SHELL_TOOL_NAME, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.exceptions import ServerException
from app.core.prompts import EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.terminal.schemas import TerminalSessionStatus
from app.core.utils.background_task_result import build_background_task_failure_result
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts
from app.core.utils.dispatcher.prepare_messages import prepare_messages
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.message_platform_outbox import MessagePlatformOutbox
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.terminal_session import TerminalSession
from app.models.user import User
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer
from tests.integration.background_tasks.test_external_work_dispatch import (
    MODEL_ID,
    PROFILE_ID,
    SESSION_ID,
    TASK_WORKER_ID,
    UID,
    WORKER_ID,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "external-test"])
@pytest.mark.parametrize("scenario", ["result-last", "assistant-last", "timeout-assistant-last", "assistant-last-retry", "checkpoint-save-failure"])
@pytest.mark.parametrize("stream_requested", [False, True], ids=["buffered", "stream-events"])
async def test_shell_external_work_resumes_same_work_with_latest_processed_history(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    scenario: str,
    stream_requested: bool,
) -> None:
    async with database_factory(
        foreign_keys=True,
        wal=True,
        name="external-work-dispatch.sqlite3",
    ) as factory:
        channel = ModelChannel(
            id=1,
            name="external-dispatch-channel",
            api_key="enc:v1:stored-test-key",
            base_url="https://llm.invalid",
            model_ids=[
                {
                    "model_id": MODEL_ID,
                    "usage": "CHAT",
                    "protocol": "OPENAI",
                    "context_window_k": 32,
                    "max_tokens": 512,
                }
            ],
        )
        profile = Profile(
            id=PROFILE_ID,
            uid=UID,
            name="external-dispatch-profile",
            configs=ProfileConfig.model_validate(
                {
                    "channel": {
                        "chat_channel": {
                            "rules": [
                                {
                                    "channel_id": 1,
                                    "model_id": MODEL_ID,
                                    "priority": 1,
                                    "weight": 1,
                                }
                            ]
                        }
                    },
                    "security": {"audit_threshold": 0},
                    "tool": {"enabled_tools": ["execute_shell"]},
                    "other": {},
                    "memory": {"enabled": False},
                }
            ).model_dump(mode="json"),
        )
        user = User(uid=UID, username="external-dispatch-user")
        session = ChatSession(
            session_id=SESSION_ID,
            uid=UID,
            profile_id=PROFILE_ID,
            source=source,
            goal_mode=False,
            stream_llm_request=False,
            show_tool_calls=False,
        )

        async with factory() as db:
            db.add_all([channel, profile, user, session])
            await db.commit()

        for module in (
            database_provider,
            dispatcher_module,
            executor_interactive_module,
            executor_lifecycle_module,
            executor_metadata_module,
            channel_cursor_module,
            external_work_module,
            notifier_module,
        ):
            monkeypatch.setattr(module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
        original_save_checkpoint = executor_interactive_module._save_interactive_work_execution_checkpoint
        checkpoint_save_failure_triggered = False

        async def save_checkpoint_with_failure(db, checkpoint, *, work, worker_id):
            nonlocal checkpoint_save_failure_triggered
            if scenario == "checkpoint-save-failure" and not checkpoint_save_failure_triggered and checkpoint.get("pending_resume_message_ids"):
                checkpoint_save_failure_triggered = True
                raise RuntimeError("fixture checkpoint save failure")
            return await original_save_checkpoint(db, checkpoint, work=work, worker_id=worker_id)

        monkeypatch.setattr(executor_interactive_module, "_save_interactive_work_execution_checkpoint", save_checkpoint_with_failure)

        async def noop_transport(*_args, **_kwargs) -> None:
            return None

        if source == "http":
            monkeypatch.setattr(executor_lifecycle_module, "send_session_event", noop_transport)
        else:
            monkeypatch.setattr(executor_lifecycle_module, "send_session_event", notifier_module.send_session_event)

        provider_payloads: list[dict] = []
        original_tool_result_message_id: int | None = None
        original_tool_result_content: str | None = None
        background_result_message_id: int | None = None
        background_result_content: str | None = None
        queued_tool_payload: dict = {}
        latest_user_content = "latest user message: do not rerun the command; only report its existing result"
        latest_assistant_content = "latest assistant reply"
        expected_task_result: dict = {}
        expected_execution_status = "failed" if scenario == "timeout-assistant-last" else "succeeded"
        expected_reply_content = "external shell timed out" if scenario == "timeout-assistant-last" else "external shell completed"
        retry_assistant_content = "assistant written after failed recovery"

        async def fake_post_json(_transformer, **kwargs):
            nonlocal background_result_message_id, background_result_content

            payload = kwargs["payload"]
            provider_payloads.append(copy.deepcopy(payload))
            request_number = len(provider_payloads)
            assert payload["model"] == MODEL_ID
            assert payload["stream"] is False
            if request_number == 1:
                return {
                    "id": "chatcmpl-external-shell",
                    "object": "chat.completion",
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "shell-call",
                                        "type": "function",
                                        "function": {
                                            "name": "execute_shell",
                                            "arguments": json.dumps(
                                                {
                                                    "command": "echo fixture",
                                                    "execution_mode": "non_interactive",
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                }

            if request_number == 3:
                assert scenario == "assistant-last-retry"
                assert payload["messages"] == provider_payloads[1]["messages"]
                assert payload["tools"] == provider_payloads[1]["tools"]
            else:
                assert request_number == 2
            messages = payload["messages"]
            assert messages[-1]["role"] == "user"
            first_payload = provider_payloads[0]
            assert messages[: len(first_payload["messages"])] == first_payload["messages"]
            assert payload["tools"] == first_payload["tools"]

            def contains_expected_content(item: dict, expected: str) -> bool:
                content = item.get("content")
                if not isinstance(content, str):
                    return False
                try:
                    parsed_content = json.loads(content)
                except json.JSONDecodeError:
                    return expected in content
                if isinstance(parsed_content, dict):
                    return any(isinstance(parsed_content.get(field), str) and expected in parsed_content[field] for field in ("user_message", "content"))
                return expected in content

            latest_user_indices = [index for index, item in enumerate(messages) if item.get("role") == "user" and contains_expected_content(item, latest_user_content)]
            latest_assistant_indices = [index for index, item in enumerate(messages) if item.get("role") == "assistant" and contains_expected_content(item, latest_assistant_content)]
            assert len(latest_user_indices) == 1
            assert len(latest_assistant_indices) == 1

            tool_messages = [item for item in messages if item.get("role") == "tool"]
            assert len(tool_messages) == 1
            tool_result = tool_messages[0]
            tool_call_id = tool_result.get("tool_call_id")
            assert isinstance(tool_call_id, str)
            assert original_tool_result_message_id is not None
            assert json.loads(tool_result["content"])["tool_result"] == queued_tool_payload
            tool_call_messages = [item for item in messages if item.get("role") == "assistant" and item.get("tool_calls")]
            assert len(tool_call_messages) == 1
            assert tool_call_messages[0]["tool_calls"][0]["id"] == tool_call_id
            assert messages.index(tool_call_messages[0]) < messages.index(tool_result)
            assert messages.index(tool_result) < latest_user_indices[0]

            resume_payload = None
            resume_indices: list[int] = []
            for index, item in enumerate(messages):
                content = item.get("content")
                if item.get("role") != "user" or not isinstance(content, str):
                    continue
                try:
                    envelope = json.loads(content)
                except json.JSONDecodeError:
                    continue
                if not isinstance(envelope, dict):
                    continue
                user_message = envelope.get("user_message")
                if not isinstance(user_message, str):
                    continue
                try:
                    candidate = json.loads(user_message)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and candidate.get("type") == "external_work_resume":
                    resume_payload = candidate
                    resume_indices.append(index)
            assert resume_payload is not None
            assert len(resume_indices) == 1
            resume_index = resume_indices[0]
            assert resume_payload["tasks"][0]["instruction"] == EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT
            assert latest_user_indices[0] < resume_index
            assert latest_user_indices[0] < latest_assistant_indices[0] < resume_index
            assert resume_index == len(messages) - 1
            assert any(item.get("role") == "user" and item.get("content") == "run the fixture shell command" for item in resume_payload["tasks"][0]["request_context"])
            assert resume_payload["tasks"] == [
                {
                    "type": "background_tool_result",
                    "ui_only": True,
                    "original_tool_result_message_id": original_tool_result_message_id,
                    "task_id": task_id,
                    "tool_name": "execute_shell",
                    "execution_status": expected_execution_status,
                    "instruction": resume_payload["tasks"][0]["instruction"],
                    "task": expected_task_result,
                    "tool_call": {
                        "id": tool_call_id,
                        "name": "execute_shell",
                        "arguments": {
                            "command": "echo fixture",
                            "execution_mode": "non_interactive",
                        },
                    },
                    "request_context": [
                        {
                            "id": _message.id,
                            "role": "user",
                            "content": "run the fixture shell command",
                        }
                    ],
                    "purpose": resume_payload["tasks"][0]["purpose"],
                }
            ]
            assert latest_assistant is not None and latest_assistant.id is not None
            async with factory() as inspection_db:
                persisted_background_results = list(
                    (
                        await inspection_db.execute(
                            select(Message)
                            .where(
                                Message.session_id == SESSION_ID,
                                Message.type == MessageType.BACKGROUND_TASK_RESULT,
                            )
                            .order_by(Message.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(persisted_background_results) == 1
                background_result_message = persisted_background_results[0]
                assert background_result_message.id is not None
                assert background_result_message.id > latest_assistant.id
                assert background_result_message.role == MessageRole.USER
                assert background_result_message.content is not None
                if background_result_message_id is None:
                    background_result_message_id = background_result_message.id
                    background_result_content = background_result_message.content
                else:
                    assert background_result_message.id == background_result_message_id
                    assert background_result_message.content == background_result_content
                assert background_result_content is not None
                assert json.loads(background_result_content) == resume_payload["tasks"][0]

                persisted_work = await inspection_db.get(SessionReplyWorkItem, work_id)
                assert persisted_work is not None
                checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(inspection_db, persisted_work.id) or {})
                assert "reload_history" not in checkpoint
                checkpoint_messages = checkpoint.get("messages")
                assert isinstance(checkpoint_messages, list)
                checkpoint_message_ids = [item.get("id") for item in checkpoint_messages if isinstance(item, dict)]
                assert background_result_message_id in checkpoint_message_ids
                assert checkpoint_message_ids[-1] == background_result_message_id

            if scenario == "assistant-last-retry" and request_number == 2:
                raise RuntimeError("fixture provider failure")
            return {
                "id": "chatcmpl-external-final",
                "object": "chat.completion",
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": expected_reply_content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
            }

        monkeypatch.setattr(OpenAIChatCompletionsTransformer, "_post_json", fake_post_json)

        async with factory() as db:
            persisted_profile = await db.get(Profile, PROFILE_ID)
            assert persisted_profile is not None
            _message, work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile=persisted_profile,
                message="run the fixture shell command",
                attachments=None,
                source=source,
                stream_requested=stream_requested,
                context_summary_events_requested=False,
                request_id="external-shell-request",
            )
            assert submission_status == "accepted"
            assert work.id is not None
            work_id = work.id

        async with factory() as db:
            claimed = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=WORKER_ID,
                lease_seconds=300,
            )
        assert claimed is not None
        assert claimed.id == work_id
        await executor_lifecycle_module.execute_session_reply_work(work_id, WORKER_ID)

        async with factory() as db:
            waiting_work = await db.get(SessionReplyWorkItem, work_id)
            tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
            messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
            outbox_items = list((await db.execute(select(MessagePlatformOutbox))).scalars().all())
            assert waiting_work is not None
            assert waiting_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert waiting_work.id == work_id
            assert len(tasks) == 1
            assert tasks[0].auto_reply is False
            assert tasks[0].status == BackgroundTaskStatus.PENDING
            assert not [message for message in messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
            assert outbox_items == []
            queued_tool_messages = [message for message in messages if message.type == MessageType.TOOL_RESULT]
            assert len(queued_tool_messages) == 1
            queued_tool_message = queued_tool_messages[0]
            assert queued_tool_message.id is not None
            original_tool_result_message_id = queued_tool_message.id
            original_tool_result_content = queued_tool_message.content
            stored_queued_tool_result = InternalMessage.model_validate_json(queued_tool_message.content or "{}")
            queued_tool_payload = json.loads(stored_queued_tool_result.content or "{}")
            assert queued_tool_payload["status"] == "queued"
            assert queued_tool_payload["task_id"] == tasks[0].id
        assert len(provider_payloads) == 1

        latest_assistant: Message | None = None
        async with factory() as db:
            latest_user = await save_message(
                db,
                SESSION_ID,
                UID,
                MessageRole.USER,
                MessageType.TEXT,
                InternalMessage.from_user_input(content=latest_user_content),
                PROFILE_ID,
                is_processed=True,
                commit=False,
            )
            if scenario == "result-last":
                latest_assistant = await save_message(
                    db,
                    SESSION_ID,
                    UID,
                    MessageRole.ASSISTANT,
                    MessageType.TEXT,
                    InternalMessage(role=MessageRole.ASSISTANT, content=latest_assistant_content),
                    PROFILE_ID,
                    is_processed=True,
                    commit=False,
                )
            await db.commit()
            assert latest_user.id is not None

        async with factory() as db:
            task = (await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == SESSION_ID))).scalars().one()
            assert task.id is not None
            claimed_task = await background_task_crud.try_claim(
                db,
                task_id=task.id,
                worker_id=TASK_WORKER_ID,
                lease_seconds=300,
            )
            assert claimed_task is not None
            if scenario == "timeout-assistant-last":
                timeout_error = "command timed out after 60 seconds"
                expected_task_result = build_background_task_failure_result(SHELL_TOOL_NAME, timeout_error)
                assert await background_task_crud.mark_failed(
                    db,
                    task_id=task.id,
                    worker_id=TASK_WORKER_ID,
                    error=timeout_error,
                    result=expected_task_result,
                    auto_reply=False,
                )
            else:
                task_result = {"content": {"stdout": "fixture", "exit_code": 0}}
                expected_task_result = task_result["content"]
                assert await background_task_crud.mark_succeeded(
                    db,
                    task_id=task.id,
                    worker_id=TASK_WORKER_ID,
                    result=task_result,
                    auto_reply=False,
                )
            task_id = task.id

        assert await resume_external_work_for_task(task_id) is True

        async with factory() as db:
            resumed_work = await db.get(SessionReplyWorkItem, work_id)
            assert resumed_work is not None
            assert resumed_work.id == work_id
            assert resumed_work.status == SessionReplyWorkStatus.READY_FOR_LLM
            execution_state = dict(resumed_work.execution_state or {})
            external_resume_context = execution_state.get("external_resume_context")
            assert isinstance(external_resume_context, list)
            assert len(external_resume_context) == 1
            assert external_resume_context[0]["task_id"] == task_id
            assert external_resume_context[0]["task"] == expected_task_result
            background_result_messages = list(
                (
                    await db.execute(
                        select(Message)
                        .where(
                            Message.session_id == SESSION_ID,
                            Message.type == MessageType.BACKGROUND_TASK_RESULT,
                        )
                        .order_by(Message.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(background_result_messages) == 0
            outbox_items = list((await db.execute(select(MessagePlatformOutbox))).scalars().all())
            assert outbox_items == []

        if scenario != "result-last":
            async with factory() as db:
                latest_assistant = await save_message(
                    db,
                    SESSION_ID,
                    UID,
                    MessageRole.ASSISTANT,
                    MessageType.TEXT,
                    InternalMessage(role=MessageRole.ASSISTANT, content=latest_assistant_content),
                    PROFILE_ID,
                    is_processed=True,
                    commit=False,
                )
                await db.commit()

        assert latest_user.id is not None
        assert latest_assistant is not None and latest_assistant.id is not None

        retry_assistant: Message | None = None
        if scenario == "assistant-last-retry":
            async with factory() as db:
                retry_claim = await session_reply_work_item_crud.claim_next(
                    db,
                    worker_id=WORKER_ID,
                    lease_seconds=300,
                )
            assert retry_claim is not None
            assert retry_claim.id == work_id
            with pytest.raises(ServerException):
                await executor_lifecycle_module.execute_session_reply_work(work_id, WORKER_ID)

            async with factory() as db:
                retry_assistant = await save_message(
                    db,
                    SESSION_ID,
                    UID,
                    MessageRole.ASSISTANT,
                    MessageType.TEXT,
                    InternalMessage(role=MessageRole.ASSISTANT, content=retry_assistant_content),
                    PROFILE_ID,
                    is_processed=True,
                    commit=False,
                )
                await db.commit()
                assert retry_assistant.id is not None

            async with factory() as db:
                assert await session_reply_work_item_crud.release_for_retry(
                    db,
                    work_id=work_id,
                    worker_id=WORKER_ID,
                    error="fixture provider failure",
                    delay_seconds=0,
                )
        elif scenario == "checkpoint-save-failure":
            async with factory() as db:
                failed_claim = await session_reply_work_item_crud.claim_next(
                    db,
                    worker_id=WORKER_ID,
                    lease_seconds=300,
                )
            assert failed_claim is not None
            assert failed_claim.id == work_id
            with pytest.raises(ServerException):
                await executor_lifecycle_module.execute_session_reply_work(work_id, WORKER_ID)
            assert checkpoint_save_failure_triggered is True

            async with factory() as db:
                failed_task = await db.get(BackgroundTask, task_id)
                failed_work = await db.get(SessionReplyWorkItem, work_id)
                failed_result_messages = list(
                    (
                        await db.execute(
                            select(Message).where(
                                Message.session_id == SESSION_ID,
                                Message.type == MessageType.BACKGROUND_TASK_RESULT,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert failed_task is not None
                assert failed_task.result == {"content": expected_task_result}
                assert failed_work is not None
                assert failed_work.status == SessionReplyWorkStatus.RUNNING
                failed_checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, failed_work.id) or {})
                assert failed_checkpoint.get("deferred_external_results") is True
                assert failed_checkpoint.get("reload_history") is True
                assert failed_result_messages == []
                assert len(provider_payloads) == 1

            async with factory() as db:
                assert await session_reply_work_item_crud.release_for_retry(
                    db,
                    work_id=work_id,
                    worker_id=WORKER_ID,
                    error="fixture checkpoint save failure",
                    delay_seconds=0,
                )

        async with factory() as db:
            resumed_claim = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=WORKER_ID,
                lease_seconds=300,
            )
        assert resumed_claim is not None
        assert resumed_claim.id == work_id
        await executor_lifecycle_module.execute_session_reply_work(work_id, WORKER_ID)

        async with factory() as db:
            final_work = await db.get(SessionReplyWorkItem, work_id)
            persisted_tasks = list((await db.execute(select(BackgroundTask))).scalars().all())
            persisted_messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
            outbox_items = list((await db.execute(select(MessagePlatformOutbox).order_by(MessagePlatformOutbox.id))).scalars().all())
            assert final_work is not None
            assert final_work.id == work_id
            assert final_work.status == SessionReplyWorkStatus.SUCCEEDED
            assert len(list((await db.execute(select(SessionReplyWorkItem))).scalars().all())) == 1
            assert len(persisted_tasks) == 1
            expected_task_status = BackgroundTaskStatus.FAILED if scenario == "timeout-assistant-last" else BackgroundTaskStatus.SUCCEEDED
            assert persisted_tasks[0].status == expected_task_status
            assert persisted_tasks[0].auto_reply is False
            assert persisted_tasks[0].result == (expected_task_result if scenario == "timeout-assistant-last" else {"content": expected_task_result})
            if scenario == "timeout-assistant-last":
                assert persisted_tasks[0].error == "command timed out after 60 seconds"
            else:
                assert persisted_tasks[0].error is None
            persisted_tool_results = [message for message in persisted_messages if message.type == MessageType.TOOL_RESULT]
            assert len(persisted_tool_results) == 1
            assert persisted_tool_results[0].id == original_tool_result_message_id
            assert persisted_tool_results[0].content == original_tool_result_content
            persisted_tool_result = InternalMessage.model_validate_json(persisted_tool_results[0].content or "{}")
            assert json.loads(persisted_tool_result.content or "{}") == queued_tool_payload
            persisted_background_results = [message for message in persisted_messages if message.type == MessageType.BACKGROUND_TASK_RESULT]
            assert len(persisted_background_results) == 1
            assert persisted_background_results[0].id == background_result_message_id
            assert persisted_background_results[0].role == MessageRole.USER
            assert persisted_background_results[0].content == background_result_content
            assert latest_user.id is not None and latest_assistant is not None and latest_assistant.id is not None
            persisted_latest_user = await db.get(Message, latest_user.id)
            persisted_latest_assistant = await db.get(Message, latest_assistant.id)
            assert persisted_latest_user is not None
            assert persisted_latest_user.role == MessageRole.USER
            assert persisted_latest_user.type == MessageType.TEXT
            assert persisted_latest_user.content == latest_user_content
            assert persisted_latest_assistant is not None
            assert persisted_latest_assistant.role == MessageRole.ASSISTANT
            assert persisted_latest_assistant.type == MessageType.TEXT
            assert persisted_latest_assistant.content == latest_assistant_content
            message_positions = {message.id: index for index, message in enumerate(persisted_messages) if message.id is not None}
            assert message_positions[original_tool_result_message_id] < message_positions[latest_user.id] < message_positions[latest_assistant.id] < message_positions[background_result_message_id]
            if retry_assistant is not None:
                assert retry_assistant.id is not None
                persisted_retry_assistant = await db.get(Message, retry_assistant.id)
                assert persisted_retry_assistant is not None
                assert persisted_retry_assistant.role == MessageRole.ASSISTANT
                assert persisted_retry_assistant.type == MessageType.TEXT
                assert persisted_retry_assistant.content == retry_assistant_content
                assert message_positions[background_result_message_id] < message_positions[retry_assistant.id]
            assert not any(message.role == MessageRole.USER and message.type == MessageType.TEXT and message.content == EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT for message in persisted_messages)
            assert final_work.result_message_id is not None
            result_message = await db.get(Message, final_work.result_message_id)
            assert result_message is not None
            assert result_message.role == MessageRole.ASSISTANT
            assert result_message.type == MessageType.TEXT
            assert result_message.content == expected_reply_content
            if source == "external-test":
                assert len(outbox_items) == 1
                outbound_event = outbox_items[0].event
                assert outbox_items[0].source == source
                assert outbound_event["type"] == "proactive_reply"
                assert outbound_event["source"] == "foreground"
                assert outbound_event["content"] == expected_reply_content
                serialized_event = json.dumps(outbound_event, ensure_ascii=False)
                assert "background_tool_result" not in serialized_event
                assert "instruction" not in serialized_event
            else:
                assert outbox_items == []

        assert len(provider_payloads) == (3 if scenario == "assistant-last-retry" else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"])
async def test_interactive_shell_auto_watch_suspends_before_another_model_request(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    command = "echo terminal-watch-fixture"
    provider_call_id = "interactive-shell-call"
    provider_payloads: list[dict] = []

    async def fake_post_json(_transformer, **kwargs):
        payload = kwargs["payload"]
        provider_payloads.append(copy.deepcopy(payload))
        assert len(provider_payloads) == 1, "interactive shell must suspend before a second model request"
        assert payload["model"] == MODEL_ID
        assert payload["stream"] is False
        assert kwargs["url"].endswith("/chat/completions" if protocol == "OPENAI" else "/responses")
        serialized_arguments = json.dumps({"command": command, "execution_mode": "interactive"}, ensure_ascii=False)
        if protocol == "OPENAI":
            return {
                "id": "chatcmpl-interactive-shell-auto-watch",
                "object": "chat.completion",
                "model": MODEL_ID,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{"id": provider_call_id, "type": "function", "function": {"name": SHELL_TOOL_NAME, "arguments": serialized_arguments}}]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        return {
            "id": "resp-interactive-shell-auto-watch",
            "object": "response",
            "model": MODEL_ID,
            "status": "completed",
            "output": [{"type": "function_call", "id": provider_call_id, "call_id": provider_call_id, "name": SHELL_TOOL_NAME, "arguments": serialized_arguments, "status": "completed"}],
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        }

    async with database_factory(foreign_keys=True, wal=True, name=f"interactive-shell-auto-watch-{protocol.lower()}.sqlite3") as factory:
        channel = ModelChannel(
            id=1,
            name="interactive-shell-auto-watch-channel",
            api_key="enc:v1:stored-test-key",
            base_url="https://llm.invalid",
            model_ids=[{"model_id": MODEL_ID, "usage": "CHAT", "protocol": protocol, "context_window_k": 32, "max_tokens": 512}],
        )
        profile = Profile(
            id=PROFILE_ID,
            uid=UID,
            name="interactive-shell-auto-watch-profile",
            configs=ProfileConfig.model_validate(
                {
                    "channel": {"chat_channel": {"rules": [{"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1}]}},
                    "security": {"audit_threshold": 0},
                    "tool": {"enabled_tools": [SHELL_TOOL_NAME]},
                    "other": {},
                    "memory": {"enabled": False},
                }
            ).model_dump(mode="json"),
        )
        user = User(uid=UID, username="interactive-shell-auto-watch-user")
        session = ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID, source="http", goal_mode=False, stream_llm_request=False, show_tool_calls=True)

        async with factory() as db:
            db.add_all([channel, profile, user, session])
            await db.commit()

        for module in (database_provider, dispatcher_module, executor_interactive_module, executor_lifecycle_module, executor_metadata_module, channel_cursor_module, external_work_module, reply_trigger_module, notifier_module):
            monkeypatch.setattr(module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
        transformer_cls = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
        monkeypatch.setattr(transformer_cls, "_post_json", fake_post_json)

        async def noop_transport(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", noop_transport)
        monkeypatch.setattr(notifier_module, "send_session_event", noop_transport)

        async with factory() as db:
            persisted_profile = await db.get(Profile, PROFILE_ID)
            assert persisted_profile is not None
            _message, work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile=persisted_profile,
                message="start the interactive fixture",
                attachments=None,
                source="http",
                stream_requested=False,
                context_summary_events_requested=False,
                request_id="interactive-shell-auto-watch-request",
            )
            assert submission_status == "accepted"
            assert work.id is not None
            work_id = work.id

        async with factory() as db:
            claimed_work = await session_reply_work_item_crud.claim_next(db, worker_id=WORKER_ID, lease_seconds=300)
            assert claimed_work is not None
            assert claimed_work.id == work_id
        await executor_lifecycle_module.execute_session_reply_work(work_id, WORKER_ID)

        async with factory() as db:
            waiting_work = await db.get(SessionReplyWorkItem, work_id)
            works = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
            tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
            messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
            assert waiting_work is not None
            assert len(works) == 1 and works[0].work_type != SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY
            assert waiting_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK and waiting_work.result_message_id is None
            assert len(tasks) == 1
            auto_task = tasks[0]
            assert auto_task.tool_name == TERMINAL_WATCH_TOOL_NAME and auto_task.status == BackgroundTaskStatus.PENDING
            assert auto_task.auto_reply is True and auto_task.reply_status == BackgroundTaskReplyStatus.PENDING
            assert auto_task.extra["auto_terminal_watch"] is True
            assert auto_task.extra["origin_work_id"] == work_id
            assert auto_task.extra["origin_work_identity"] == waiting_work.dedupe_key
            assert auto_task.extra.get("terminal_watch_subscription") is not True
            terminal_session_id = auto_task.extra["terminal_session_id"]
            terminal_session = await db.get(TerminalSession, terminal_session_id)
            assert terminal_session is not None
            assert terminal_session.status == TerminalSessionStatus.STARTING
            assert terminal_session.command == command
            tool_call_messages = [message for message in messages if message.type == MessageType.TOOL_CALL]
            assert len(tool_call_messages) == 1
            persisted_tool_call = InternalMessage.model_validate_json(tool_call_messages[0].content or "{}")
            assert persisted_tool_call.role == MessageRole.ASSISTANT
            assert persisted_tool_call.tool_calls is not None and len(persisted_tool_call.tool_calls) == 1
            persisted_shell_call = persisted_tool_call.tool_calls[0]
            assert persisted_shell_call.name == SHELL_TOOL_NAME
            assert persisted_shell_call.arguments == {"command": command, "execution_mode": "interactive"}
            internal_tool_call_id = persisted_shell_call.id
            assert isinstance(internal_tool_call_id, str) and internal_tool_call_id
            tool_results = [message for message in messages if message.type == MessageType.TOOL_RESULT]
            assert len(tool_results) == 1
            assert tool_results[0].id is not None
            tool_result = InternalMessage.model_validate_json(tool_results[0].content or "{}")
            handoff = json.loads(tool_result.content or "{}")
            assert tool_result.tool_call_id == internal_tool_call_id and tool_result.tool_execution_status == "succeeded"
            assert handoff["terminal_session_id"] == terminal_session_id and handoff["status"] == TerminalSessionStatus.STARTING.value
            assert handoff["output_stream"] == "merged_stdout_stderr"
            assert "queued" not in json.dumps(handoff, ensure_ascii=False)
            execution_state = waiting_work.execution_state or {}
            assert execution_state["pending_tools"] == []
            waits = execution_state["terminal_watch_waits"]
            assert len(waits) == 1
            wait = waits[0]
            assert wait["task_id"] == auto_task.id and wait["tool_call_id"] == internal_tool_call_id
            assert wait["tool_name"] == SHELL_TOOL_NAME and wait["tool_result_message_id"] == tool_results[0].id
            assert wait["terminal_watch"] is True and wait["terminal_auto_watch"] is True
            assert wait["terminal_session_id"] == terminal_session_id
            auto_task_id = auto_task.id
            assert auto_task_id is not None
            assert len(provider_payloads) == 1
            assert not any(message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT for message in messages)
            assert not any(message.type == MessageType.BACKGROUND_TASK_RESULT for message in messages)
            assert list((await db.execute(select(MessagePlatformOutbox))).scalars().all()) == []

        timeout_content = {"terminal_session_id": terminal_session_id, "event": "timeout", "after_offset": 0, "session_status": "running", "exit_code": None, "failure_reason": None, "output": "", "start_offset": 0, "next_offset": 0, "latest_offset": 0, "output_truncated": False, "eof": False}
        async with factory() as db:
            claimed_auto = await background_task_crud.try_claim(db, task_id=auto_task_id, worker_id=TASK_WORKER_ID, lease_seconds=300)
            assert claimed_auto is not None
            assert await complete_terminal_watch_round(
                db,
                task=claimed_auto,
                worker_id=TASK_WORKER_ID,
                content=timeout_content,
            )
            completed_auto = await db.get(BackgroundTask, auto_task_id)
            assert completed_auto is not None
            assert completed_auto.result["content"] == timeout_content
            next_task_id = completed_auto.extra.get("next_auto_watch_task_id")
            assert isinstance(next_task_id, int)
            next_task = await db.get(BackgroundTask, next_task_id)
            assert next_task is not None
            assert next_task.status == BackgroundTaskStatus.PENDING
            assert next_task.extra["origin_work_id"] == work_id
            assert next_task.extra["origin_work_identity"] == waiting_work.dedupe_key

        assert await resume_external_work_for_task(auto_task_id) is True
        assert len(provider_payloads) == 1

        async with factory() as db:
            resumed_work = await db.get(SessionReplyWorkItem, work_id)
            assert resumed_work is not None
            assert resumed_work.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert len(list((await db.execute(select(SessionReplyWorkItem))).scalars().all())) == 1
            assert not any(message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT for message in (await db.execute(select(Message).where(Message.session_id == SESSION_ID))).scalars().all())
            assert not any(work_item.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY for work_item in (await db.execute(select(SessionReplyWorkItem))).scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"])
async def test_external_work_repeated_resumes_append_history_and_resume_logs(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    initial_request = "run shell-1, then shell-2"
    first_progress_user = "查看进度"
    first_progress_assistant = "仍在运行"
    second_progress_user = "第二次查看进度"
    second_progress_assistant = "第二次仍在运行"
    long_stdout = "SHELL_ONE_LONG_STDOUT_" + "|".join(f"unique-line-{index:04d}" for index in range(300))
    assert len(long_stdout) > 2000

    provider_payloads: list[dict] = []
    captured_logs: list[dict] = []

    def provider_items(payload: dict) -> list[dict]:
        return payload["messages"] if protocol == "OPENAI" else payload["input"]

    def provider_user_text(item: dict) -> str | None:
        if item.get("role") != "user":
            return None
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(part["text"] for part in content if isinstance(part, dict) and part.get("type") == "input_text" and isinstance(part.get("text"), str))
        return None

    def provider_user_message_text(item: dict) -> str | None:
        content = provider_user_text(item)
        if content is None:
            return None
        try:
            envelope = json.loads(content)
        except json.JSONDecodeError:
            return None
        if not isinstance(envelope, dict):
            return None
        user_message = envelope.get("user_message")
        if isinstance(user_message, str):
            return user_message
        if isinstance(user_message, list):
            return "".join(part.get("text", "") for part in user_message if isinstance(part, dict) and isinstance(part.get("text"), str))
        return None

    def provider_assistant_message_text(item: dict) -> str | None:
        if item.get("role") != "assistant":
            return None
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str) and part.get("type") in {"text", "input_text", "output_text"})
        return None

    def provider_message_text(item: dict) -> tuple[str, str] | None:
        role = item.get("role")
        if role == "user":
            text = provider_user_message_text(item)
        elif role == "assistant":
            text = provider_assistant_message_text(item)
        else:
            return None
        return (role, text) if isinstance(text, str) else None

    def background_marker(item: dict) -> dict | None:
        text = provider_user_text(item)
        if text is None:
            return None
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(envelope, dict):
            return None
        raw_marker = envelope.get("user_message")
        if not isinstance(raw_marker, str):
            return None
        try:
            marker = json.loads(raw_marker)
        except json.JSONDecodeError:
            return None
        if not isinstance(marker, dict):
            return None
        if marker.get("type") == "background_tool_result":
            return marker
        if marker.get("type") != "external_work_resume":
            return None
        tasks = marker.get("tasks")
        if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
            return None
        task = tasks[0]
        return task if task.get("type") == "background_tool_result" else None

    def provider_marker_task_ids(items: list[dict]) -> list[int]:
        return [marker["task_id"] for item in items if (marker := background_marker(item)) is not None]

    def provider_tool_calls(items: list[dict]) -> list[tuple[str, dict, str]]:
        calls: list[tuple[str, dict, str]] = []
        if protocol == "OPENAI":
            for item in items:
                if item.get("role") != "assistant":
                    continue
                for call in item.get("tool_calls") or []:
                    function = call.get("function") or {}
                    calls.append((function["name"], json.loads(function["arguments"]), call["id"]))
            return calls
        for item in items:
            if item.get("type") != "function_call":
                continue
            call_id = item.get("call_id") or item.get("id")
            calls.append((item["name"], json.loads(item["arguments"]), call_id))
        return calls

    def provider_tool_result_ids(items: list[dict]) -> list[str]:
        if protocol == "OPENAI":
            return [item["tool_call_id"] for item in items if item.get("role") == "tool"]
        return [item["call_id"] for item in items if item.get("type") == "function_call_output"]

    def provider_response(request_number: int, tool_call: tuple[str, dict, str] | None = None, content: str | None = None) -> dict:
        if tool_call is None:
            assert content is not None
            openai_message = {"role": "assistant", "content": content}
            responses_output = [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": content}],
                }
            ]
            finish_reason = "stop"
        else:
            name, arguments, call_id = tool_call
            serialized_arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            openai_message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": serialized_arguments},
                    }
                ],
            }
            responses_output = [
                {
                    "type": "function_call",
                    "id": call_id,
                    "call_id": call_id,
                    "name": name,
                    "arguments": serialized_arguments,
                    "status": "completed",
                }
            ]
            finish_reason = "tool_calls"

        if protocol == "OPENAI":
            return {
                "id": f"chatcmpl-repeated-resume-{request_number}",
                "object": "chat.completion",
                "model": MODEL_ID,
                "choices": [{"index": 0, "message": openai_message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        return {
            "id": f"resp-repeated-resume-{request_number}",
            "object": "response",
            "model": MODEL_ID,
            "status": "completed",
            "output": responses_output,
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        }

    async def fake_post_json(_transformer, **kwargs):
        payload = kwargs["payload"]
        provider_payloads.append(copy.deepcopy(payload))
        request_number = len(provider_payloads)
        assert payload["model"] == MODEL_ID
        assert payload["stream"] is False
        assert kwargs["url"].endswith("/chat/completions" if protocol == "OPENAI" else "/responses")
        if request_number == 1:
            return provider_response(
                request_number,
                (SHELL_TOOL_NAME, {"command": "shell-1", "execution_mode": "non_interactive"}, "shell-call-1"),
            )
        if request_number == 2:
            return provider_response(request_number, (MANAGE_TODO_TOOL_NAME, {"operation": "read"}, "todo-call"))
        if request_number == 3:
            return provider_response(
                request_number,
                (SHELL_TOOL_NAME, {"command": "shell-2", "execution_mode": "non_interactive"}, "shell-call-2"),
            )
        assert request_number == 4
        return provider_response(request_number, content="both external shells completed")

    async def save_progress_messages(user_content: str, assistant_content: str) -> None:
        async with factory() as db:
            await save_message(
                db,
                SESSION_ID,
                UID,
                MessageRole.USER,
                MessageType.TEXT,
                InternalMessage.from_user_input(content=user_content),
                PROFILE_ID,
                is_processed=True,
                commit=False,
            )
            await save_message(
                db,
                SESSION_ID,
                UID,
                MessageRole.ASSISTANT,
                MessageType.TEXT,
                InternalMessage(role=MessageRole.ASSISTANT, content=assistant_content),
                PROFILE_ID,
                is_processed=True,
                commit=False,
            )
            await db.commit()

    async def claim_and_execute(work_id: int) -> None:
        async with factory() as db:
            claimed_work = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=WORKER_ID,
                lease_seconds=300,
            )
            assert claimed_work is not None
            assert claimed_work.id == work_id
        await executor_lifecycle_module.execute_session_reply_work(work_id, WORKER_ID)

    async def complete_task_and_resume(task_id: int, result: dict) -> None:
        async with factory() as db:
            claimed_task = await background_task_crud.try_claim(
                db,
                task_id=task_id,
                worker_id=TASK_WORKER_ID,
                lease_seconds=300,
            )
            assert claimed_task is not None
            assert claimed_task.status == BackgroundTaskStatus.RUNNING
            assert await background_task_crud.mark_succeeded(
                db,
                task_id=task_id,
                worker_id=TASK_WORKER_ID,
                result=result,
                auto_reply=False,
            )
        assert await resume_external_work_for_task(task_id) is True

    async with database_factory(
        foreign_keys=True,
        wal=True,
        name=f"external-work-repeated-resume-{protocol.lower()}.sqlite3",
    ) as factory:
        channel = ModelChannel(
            id=1,
            name="external-repeated-resume-channel",
            api_key="enc:v1:stored-test-key",
            base_url="https://llm.invalid",
            model_ids=[
                {
                    "model_id": MODEL_ID,
                    "usage": "CHAT",
                    "protocol": protocol,
                    "context_window_k": 32,
                    "max_tokens": 512,
                }
            ],
        )
        profile = Profile(
            id=PROFILE_ID,
            uid=UID,
            name="external-repeated-resume-profile",
            configs=ProfileConfig.model_validate(
                {
                    "channel": {
                        "chat_channel": {
                            "rules": [
                                {
                                    "channel_id": 1,
                                    "model_id": MODEL_ID,
                                    "priority": 1,
                                    "weight": 1,
                                }
                            ]
                        }
                    },
                    "security": {"audit_threshold": 0},
                    "tool": {"enabled_tools": [SHELL_TOOL_NAME]},
                    "other": {},
                    "memory": {"enabled": False},
                }
            ).model_dump(mode="json"),
        )
        user = User(uid=UID, username="external-repeated-resume-user")
        session = ChatSession(
            session_id=SESSION_ID,
            uid=UID,
            profile_id=PROFILE_ID,
            source="http",
            goal_mode=False,
            max_turns=10,
            stream_llm_request=False,
            show_tool_calls=True,
        )

        async with factory() as db:
            db.add_all([channel, profile, user, session])
            await db.commit()

        for module in (
            database_provider,
            dispatcher_module,
            background_dispatcher_module,
            executor_interactive_module,
            executor_lifecycle_module,
            executor_metadata_module,
            channel_cursor_module,
            external_work_module,
            reply_trigger_module,
            notifier_module,
        ):
            monkeypatch.setattr(module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
        transformer_cls = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
        monkeypatch.setattr(transformer_cls, "_post_json", fake_post_json)

        async def noop_transport(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", noop_transport)
        monkeypatch.setattr(notifier_module, "send_session_event", noop_transport)

        def capture_log(message) -> None:
            record = message.record
            captured_logs.append(
                {
                    "level": record["level"].name,
                    "message": record["message"],
                    "extra": copy.deepcopy(record["extra"]),
                }
            )

        sink_id = loguru_logger.add(capture_log, level="INFO")
        try:
            async with factory() as db:
                persisted_profile = await db.get(Profile, PROFILE_ID)
                assert persisted_profile is not None
                _message, work, submission_status, _events = await session_reply_queue_manager.submit_user_message(
                    db,
                    uid=UID,
                    session_id=SESSION_ID,
                    profile=persisted_profile,
                    message=initial_request,
                    attachments=None,
                    source="http",
                    stream_requested=False,
                    context_summary_events_requested=False,
                    request_id="external-repeated-resume-request",
                )
                assert submission_status == "accepted"
                assert work.id is not None
                work_id = work.id

            await claim_and_execute(work_id)

            async with factory() as db:
                waiting_work = await db.get(SessionReplyWorkItem, work_id)
                tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
                assert waiting_work is not None
                assert waiting_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
                assert len(tasks) == 1
                task1_id = tasks[0].id
                assert task1_id is not None
                assert tasks[0].tool_name == SHELL_TOOL_NAME
                assert tasks[0].status == BackgroundTaskStatus.PENDING

            await save_progress_messages(first_progress_user, first_progress_assistant)
            await complete_task_and_resume(
                task1_id,
                {"content": {"stdout": long_stdout, "stderr": "", "exit_code": 0}},
            )
            await claim_and_execute(work_id)

            async with factory() as db:
                waiting_work = await db.get(SessionReplyWorkItem, work_id)
                tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
                assert waiting_work is not None
                assert waiting_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
                assert len(tasks) == 2
                assert tasks[0].id == task1_id
                assert tasks[0].status == BackgroundTaskStatus.SUCCEEDED
                task2_id = tasks[1].id
                assert task2_id is not None
                assert task2_id != task1_id
                assert tasks[1].tool_name == SHELL_TOOL_NAME
                assert tasks[1].status == BackgroundTaskStatus.PENDING

            assert len(provider_payloads) == 3
            await save_progress_messages(second_progress_user, second_progress_assistant)
            await complete_task_and_resume(
                task2_id,
                {"content": {"stdout": "shell-2-complete", "stderr": "", "exit_code": 0}},
            )
            await claim_and_execute(work_id)

            assert len(provider_payloads) == 4
            request_items = [provider_items(payload) for payload in provider_payloads]
            assert request_items[2][: len(request_items[1])] == request_items[1]
            assert request_items[3][: len(request_items[2])] == request_items[2]
            assert provider_marker_task_ids(request_items[0]) == []
            assert provider_marker_task_ids(request_items[1]) == [task1_id]
            assert provider_marker_task_ids(request_items[2]) == [task1_id]
            assert provider_marker_task_ids(request_items[3]) == [task1_id, task2_id]
            assert long_stdout in json.dumps(request_items[3], ensure_ascii=False)
            assert sum(provider_message_text(item) == ("user", first_progress_user) for item in request_items[3]) == 1
            assert sum(provider_message_text(item) == ("assistant", first_progress_assistant) for item in request_items[3]) == 1
            assert sum(provider_message_text(item) == ("user", second_progress_user) for item in request_items[3]) == 1
            assert sum(provider_message_text(item) == ("assistant", second_progress_assistant) for item in request_items[3]) == 1

            expected_calls = [
                (SHELL_TOOL_NAME, {"command": "shell-1", "execution_mode": "non_interactive"}),
                (MANAGE_TODO_TOOL_NAME, {"operation": "read"}),
                (SHELL_TOOL_NAME, {"command": "shell-2", "execution_mode": "non_interactive"}),
            ]
            assert [(name, arguments) for name, arguments, _call_id in provider_tool_calls(request_items[0])] == []
            assert [(name, arguments) for name, arguments, _call_id in provider_tool_calls(request_items[1])] == expected_calls[:1]
            assert [(name, arguments) for name, arguments, _call_id in provider_tool_calls(request_items[2])] == expected_calls[:2]
            assert [(name, arguments) for name, arguments, _call_id in provider_tool_calls(request_items[3])] == expected_calls
            for items in request_items:
                call_ids = [call_id for _name, _arguments, call_id in provider_tool_calls(items)]
                result_ids = provider_tool_result_ids(items)
                assert all(isinstance(call_id, str) and call_id for call_id in call_ids)
                assert all(isinstance(result_id, str) and result_id for result_id in result_ids)
                assert len(call_ids) == len(set(call_ids))
                assert len(result_ids) == len(set(result_ids))
                assert call_ids == result_ids

            async with factory() as db:
                final_work = await db.get(SessionReplyWorkItem, work_id)
                tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
                persisted_messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
                assert final_work is not None
                assert final_work.status == SessionReplyWorkStatus.SUCCEEDED
                assert len(tasks) == 2
                assert [task.id for task in tasks] == [task1_id, task2_id]
                assert all(task.status == BackgroundTaskStatus.SUCCEEDED for task in tasks)
                assert tasks[0].result == {"content": {"stdout": long_stdout, "stderr": "", "exit_code": 0}}
                assert tasks[1].result == {"content": {"stdout": "shell-2-complete", "stderr": "", "exit_code": 0}}
                assert tasks[0].tool_call_id == provider_tool_calls(request_items[1])[0][2]
                assert tasks[1].tool_call_id == provider_tool_calls(request_items[3])[-1][2]
                assert sum(message.content == first_progress_user for message in persisted_messages) == 1
                assert sum(message.content == first_progress_assistant for message in persisted_messages) == 1
                assert sum(message.content == second_progress_user for message in persisted_messages) == 1
                assert sum(message.content == second_progress_assistant for message in persisted_messages) == 1
                markers = [message for message in persisted_messages if message.type == MessageType.BACKGROUND_TASK_RESULT]
                assert len(markers) == 2
                marker_payloads = [json.loads(message.content or "{}") for message in markers]
                assert {payload["task_id"] for payload in marker_payloads} == {task1_id, task2_id}
                assert all(payload["ui_only"] is True for payload in marker_payloads)
                marker_by_task = {payload["task_id"]: payload for payload in marker_payloads}
                assert marker_by_task[task1_id]["task"]["stdout"] == long_stdout
                assert marker_by_task[task2_id]["task"]["stdout"] == "shell-2-complete"
                marker_id_by_task = {payload["task_id"]: message.id for message, payload in zip(markers, marker_payloads, strict=True)}
                assert all(marker_id_by_task[task_id] is not None for task_id in (task1_id, task2_id))
                result_message = await db.get(Message, final_work.result_message_id)
                assert result_message is not None
                assert result_message.role == MessageRole.ASSISTANT
                assert result_message.type == MessageType.TEXT
                assert result_message.content == "both external shells completed"

            before_duplicate_resume = (len(persisted_messages), len(tasks), [message.content for message in markers])
            assert await resume_external_work_for_task(task1_id) is False
            assert await resume_external_work_for_task(task2_id) is False
            async with factory() as db:
                after_messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
                after_tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
                assert (len(after_messages), len(after_tasks), [message.content for message in after_messages if message.type == MessageType.BACKGROUND_TASK_RESULT]) == before_duplicate_resume

                persisted_profile = await db.get(Profile, PROFILE_ID)
                assert persisted_profile is not None
                cfg = ProfileConfig.model_validate(persisted_profile.configs)
                restored_messages = await prepare_messages(
                    db,
                    SESSION_ID,
                    UID,
                    persisted_profile,
                    cfg,
                    None,
                    "",
                    False,
                    context_window_k=32,
                    max_tokens=512,
                    tools=[],
                    processed_only=True,
                )
                restored_provider = transformer_cls.to_provider(
                    materialize_user_environment_prompts(restored_messages),
                    model_id=MODEL_ID,
                    channel_id=1,
                )
                assert restored_provider[: len(request_items[3])] == request_items[3]
                assert len(restored_provider[len(request_items[3]) :]) <= 1
                if len(restored_provider) > len(request_items[3]):
                    assert restored_provider[-1].get("role") == "assistant"

            resume_prefix = "External work resume notice in model request: "
            resume_records = [record for record in captured_logs if record["extra"].get("resume_message_id") is not None]
            assert len(resume_records) == 2
            logged_task_ids: list[int] = []
            for record in resume_records:
                assert record["level"] == "INFO"
                assert record["message"].startswith(resume_prefix)
                logged_message = json.loads(record["message"][len(resume_prefix) :])
                assert set(logged_message) == {"role", "content"}
                assert logged_message["role"] == "user"
                logged_content = logged_message["content"]
                assert isinstance(logged_content, str)
                logged_marker = background_marker({"role": "user", "content": logged_content})
                assert logged_marker is not None
                task_id = logged_marker["task_id"]
                logged_task_ids.append(task_id)
                assert task_id in marker_id_by_task
                assert record["extra"]["resume_message_id"] == marker_id_by_task[task_id]
                if task_id == task1_id:
                    assert logged_marker["task"]["stdout"] == long_stdout
                    assert long_stdout in record["message"]
                else:
                    assert task_id == task2_id
                    assert logged_marker["task"]["stdout"] == "shell-2-complete"
                assert "api_key" not in record["message"]
                assert "test-key" not in record["message"]
                assert '"messages"' not in record["message"]
                assert '"input"' not in record["message"]
                matching_http_messages = [item for payload in provider_payloads for item in provider_items(payload) if provider_user_text(item) == logged_content]
                assert matching_http_messages
                assert all(item.get("role") == logged_message["role"] and provider_user_text(item) == logged_content for item in matching_http_messages)
            assert sorted(logged_task_ids) == sorted([task1_id, task2_id])
            assert len(set(logged_task_ids)) == 2
        finally:
            loguru_logger.remove(sink_id)
