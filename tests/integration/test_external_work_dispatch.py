import json

import pytest
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
from app.core.background_tasks.reply_trigger import trigger_background_task_reply
from app.core.constants import TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.prompts import EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.terminal.schemas import TerminalSessionStatus
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.core.utils.background_task_result import build_background_task_success_result
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.message_platform_outbox import MessagePlatformOutbox
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.user import User
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer

UID = "external-dispatch-user"
SESSION_ID = "external-dispatch-session"
PROFILE_ID = 1
MODEL_ID = "external-dispatch-model"
WORKER_ID = "external-dispatch-worker"
TASK_WORKER_ID = "external-task-worker"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "external-test"])
async def test_shell_external_work_resumes_same_work_with_latest_processed_history(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
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
        ):
            monkeypatch.setattr(module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")

        async def noop_transport(*_args, **_kwargs) -> None:
            return None

        if source == "http":
            monkeypatch.setattr(executor_lifecycle_module, "send_session_event", noop_transport)
        else:
            monkeypatch.setattr(notifier_module, "AsyncSessionLocal", factory)
            monkeypatch.setattr(executor_lifecycle_module, "send_session_event", notifier_module.send_session_event)

        provider_payloads: list[dict] = []
        original_tool_result_message_id: int | None = None
        queued_tool_payload: dict = {}

        async def fake_post_json(_transformer, **kwargs):
            payload = kwargs["payload"]
            provider_payloads.append(payload)
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

            assert request_number == 2
            messages = payload["messages"]

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

            assert any(item.get("role") == "user" and contains_expected_content(item, "latest user message") for item in messages)
            assert any(item.get("role") == "assistant" and contains_expected_content(item, "latest assistant reply") for item in messages)

            tool_messages = [item for item in messages if item.get("role") == "tool"]
            assert len(tool_messages) == 1
            tool_result = tool_messages[0]
            tool_call_id = tool_result.get("tool_call_id")
            assert isinstance(tool_call_id, str)
            assert original_tool_result_message_id is not None
            assert json.loads(tool_result["content"]) == queued_tool_payload

            resume_payload = None
            for item in messages:
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
                    break
            assert resume_payload is not None
            assert any(item.get("role") == "user" and item.get("content") == "run the fixture shell command" for item in resume_payload["tasks"][0]["request_context"])
            assert resume_payload["tasks"] == [
                {
                    "type": "background_tool_result",
                    "ui_only": True,
                    "original_tool_result_message_id": original_tool_result_message_id,
                    "task_id": task_id,
                    "tool_name": "execute_shell",
                    "execution_status": "succeeded",
                    "instruction": resume_payload["tasks"][0]["instruction"],
                    "task": {"stdout": "fixture", "exit_code": 0},
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
            return {
                "id": "chatcmpl-external-final",
                "object": "chat.completion",
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "external shell completed"},
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
                stream_requested=False,
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
            stored_queued_tool_result = InternalMessage.model_validate_json(queued_tool_message.content or "{}")
            queued_tool_payload = json.loads(stored_queued_tool_result.content or "{}")
            assert queued_tool_payload["status"] == "queued"
            assert queued_tool_payload["task_id"] == tasks[0].id
        assert len(provider_payloads) == 1

        async with factory() as db:
            latest_user = await save_message(
                db,
                SESSION_ID,
                UID,
                MessageRole.USER,
                MessageType.TEXT,
                InternalMessage.from_user_input(content="latest user message"),
                PROFILE_ID,
                is_processed=True,
                commit=False,
            )
            latest_assistant = await save_message(
                db,
                SESSION_ID,
                UID,
                MessageRole.ASSISTANT,
                MessageType.TEXT,
                InternalMessage(role=MessageRole.ASSISTANT, content="latest assistant reply"),
                PROFILE_ID,
                is_processed=True,
                commit=False,
            )
            await db.commit()
            assert latest_user.id is not None
            assert latest_assistant.id is not None

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
            assert await background_task_crud.mark_succeeded(
                db,
                task_id=task.id,
                worker_id=TASK_WORKER_ID,
                result={"content": {"stdout": "fixture", "exit_code": 0}},
                auto_reply=False,
            )
            task_id = task.id

        assert await resume_external_work_for_task(task_id) is True

        async with factory() as db:
            resumed_work = await db.get(SessionReplyWorkItem, work_id)
            assert resumed_work is not None
            assert resumed_work.id == work_id
            assert resumed_work.status == SessionReplyWorkStatus.READY_FOR_LLM
            outbox_items = list((await db.execute(select(MessagePlatformOutbox))).scalars().all())
            assert outbox_items == []
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
            assert persisted_tasks[0].status == BackgroundTaskStatus.SUCCEEDED
            assert persisted_tasks[0].auto_reply is False
            assert persisted_tasks[0].result == {"content": {"stdout": "fixture", "exit_code": 0}}
            assert len([message for message in persisted_messages if message.type == MessageType.BACKGROUND_TASK_RESULT]) == 1
            persisted_tool_results = [message for message in persisted_messages if message.type == MessageType.TOOL_RESULT]
            assert len(persisted_tool_results) == 1
            assert persisted_tool_results[0].id == original_tool_result_message_id
            persisted_tool_result = InternalMessage.model_validate_json(persisted_tool_results[0].content or "{}")
            assert json.loads(persisted_tool_result.content or "{}") == queued_tool_payload
            assert final_work.result_message_id is not None
            result_message = await db.get(Message, final_work.result_message_id)
            assert result_message is not None
            assert result_message.role == MessageRole.ASSISTANT
            assert result_message.type == MessageType.TEXT
            assert result_message.content == "external shell completed"
            if source == "external-test":
                assert len(outbox_items) == 1
                outbound_event = outbox_items[0].event
                assert outbox_items[0].source == source
                assert outbound_event["type"] == "proactive_reply"
                assert outbound_event["source"] == "foreground"
                assert outbound_event["content"] == "external shell completed"
                serialized_event = json.dumps(outbound_event, ensure_ascii=False)
                assert "background_tool_result" not in serialized_event
                assert "instruction" not in serialized_event
            else:
                assert outbox_items == []

        assert len(provider_payloads) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"])
@pytest.mark.parametrize("event", ["timeout", "process_exit"])
@pytest.mark.parametrize("legacy_snapshot", ["missing", "empty", "stale"])
async def test_terminal_auto_watch_result_round_trips_through_queue_and_openai_protocol(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    event: str,
    legacy_snapshot: str,
) -> None:
    terminal_session_id = "terminal-auto-watch-0123456789abcdefghijklm"
    assert len(terminal_session_id) == 43
    session_status = "running" if event == "timeout" else "exited"
    exit_code = None if event == "timeout" else 0
    observed_result = {
        "terminal_session_id": terminal_session_id,
        "event": event,
        "after_offset": 0,
        "session_status": session_status,
        "exit_code": exit_code,
        "failure_reason": None,
        "output": "",
        "start_offset": 0,
        "next_offset": 0,
        "latest_offset": 0,
        "output_truncated": False,
        "eof": event == "process_exit",
    }

    async with database_factory(
        foreign_keys=True,
        wal=True,
        name=f"terminal-auto-watch-{protocol.lower()}-{event}-{legacy_snapshot}.sqlite3",
    ) as factory:
        channel = ModelChannel(
            id=1,
            name="terminal-auto-watch-channel",
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
            name="terminal-auto-watch-profile",
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
            source="external-test",
            goal_mode=False,
            stream_llm_request=False,
            show_tool_calls=True,
        )

        real_tool_result = InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="real-call",
            content=json.dumps(
                {"stdout": "real-output", "stderr": "", "exit_code": 0},
                ensure_ascii=False,
            ),
            tool_execution_status="succeeded",
        )
        real_tool_result_json = real_tool_result.model_dump_json(exclude_none=True)
        real_assistant = InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[
                InternalToolCall(
                    id="real-call",
                    name="execute_shell",
                    arguments={"command": "echo real-output"},
                )
            ],
        )

        async with factory() as db:
            db.add_all([channel, profile, user, session])
            await db.flush()
            db.add_all(
                [
                    Message(
                        session_id=SESSION_ID,
                        uid=UID,
                        profile_id=PROFILE_ID,
                        role=MessageRole.USER,
                        type=MessageType.TEXT,
                        content="original terminal task",
                        is_processed=True,
                    ),
                    Message(
                        session_id=SESSION_ID,
                        uid=UID,
                        profile_id=PROFILE_ID,
                        role=MessageRole.ASSISTANT,
                        type=MessageType.TOOL_CALL,
                        content=real_assistant.model_dump_json(exclude_none=True),
                        is_processed=True,
                    ),
                    Message(
                        session_id=SESSION_ID,
                        uid=UID,
                        profile_id=PROFILE_ID,
                        role=MessageRole.TOOL,
                        type=MessageType.TOOL_RESULT,
                        content=real_tool_result_json,
                        is_processed=True,
                    ),
                    Message(
                        session_id=SESSION_ID,
                        uid=UID,
                        profile_id=PROFILE_ID,
                        role=MessageRole.USER,
                        type=MessageType.TEXT,
                        content="stop the running process and do not rerun it",
                        is_processed=True,
                    ),
                    Message(
                        session_id=SESSION_ID,
                        uid=UID,
                        profile_id=PROFILE_ID,
                        role=MessageRole.ASSISTANT,
                        type=MessageType.TEXT,
                        content="Understood, I will stop and not rerun it.",
                        is_processed=True,
                    ),
                ]
            )
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

        async def noop_transport(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", noop_transport)
        monkeypatch.setattr(notifier_module, "send_session_event", noop_transport)

        async with factory() as db:
            await ensure_terminal_auto_watch(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile_id=PROFILE_ID,
                terminal_session_id=terminal_session_id,
                after_offset=0,
                session_status=TerminalSessionStatus.RUNNING,
            )
            tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
            assert len(tasks) == 1
            task = tasks[0]
            assert task.id is not None
            assert task.status == BackgroundTaskStatus.PENDING
            assert task.tool_name == TERMINAL_WATCH_TOOL_NAME
            assert task.auto_reply is True
            assert task.arguments == {
                "terminal_session_id": terminal_session_id,
                "after_offset": 0,
                "condition": "process_exit",
            }
            assert task.extra.get("auto_terminal_watch") is True
            assert task.extra.get("terminal_session_id") == terminal_session_id
            assert "submission_context" not in task.extra
            task_id = task.id
            watch_tool_call_id = task.tool_call_id
            assert watch_tool_call_id.startswith("terminal-auto-watch:")

            if legacy_snapshot == "empty":
                task.extra = {**task.extra, "submission_context": []}
            elif legacy_snapshot == "stale":
                task.extra = {
                    **task.extra,
                    "submission_context": [{"role": "user", "content": "obsolete-snapshot-sentinel"}],
                }
            if legacy_snapshot != "missing":
                db.add(task)
                await db.commit()

        expected_task_result = build_background_task_success_result(
            TERMINAL_WATCH_TOOL_NAME,
            observed_result,
        )
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
                result=expected_task_result,
                auto_reply=True,
            )

        await trigger_background_task_reply(task_id)
        await trigger_background_task_reply(task_id)

        async with factory() as db:
            result_events = list(
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
            assert len(result_events) == 1
            result_event = result_events[0]
            assert result_event.role == MessageRole.USER
            assert result_event.id is not None
            result_event_id = result_event.id

            summary_work_items = list(
                (
                    await db.execute(
                        select(SessionReplyWorkItem)
                        .where(
                            SessionReplyWorkItem.source_id == str(task_id),
                        )
                        .order_by(SessionReplyWorkItem.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(summary_work_items) == 1
            summary_work = summary_work_items[0]
            assert summary_work.status == SessionReplyWorkStatus.READY_FOR_LLM
            assert summary_work.id is not None
            work_id = summary_work.id

            db.add_all(
                [
                    Message(
                        session_id=SESSION_ID,
                        uid=UID,
                        profile_id=PROFILE_ID,
                        role=MessageRole.USER,
                        type=MessageType.TEXT,
                        content="future-user-sentinel",
                        is_processed=False,
                    ),
                    Message(
                        session_id=SESSION_ID,
                        uid=UID,
                        profile_id=PROFILE_ID,
                        role=MessageRole.ASSISTANT,
                        type=MessageType.TEXT,
                        content="future-assistant-sentinel",
                        is_processed=True,
                    ),
                ]
            )
            await db.commit()

        provider_payloads: list[dict] = []

        async def fake_post_json(_transformer, **kwargs):
            payload = kwargs["payload"]
            provider_payloads.append(payload)
            assert payload["model"] == MODEL_ID
            assert payload["stream"] is False
            assert kwargs["url"].endswith("/chat/completions" if protocol == "OPENAI" else "/responses")
            if protocol == "OPENAI":
                return {
                    "id": "chatcmpl-terminal-auto-watch",
                    "object": "chat.completion",
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "terminal observation acknowledged",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                }
            return {
                "id": "resp-terminal-auto-watch",
                "object": "response",
                "model": MODEL_ID,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "terminal observation acknowledged",
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            }

        transformer_cls = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
        monkeypatch.setattr(transformer_cls, "_post_json", fake_post_json)

        async with factory() as db:
            claimed_work = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=WORKER_ID,
                lease_seconds=300,
            )
            assert claimed_work is not None
            assert claimed_work.id == work_id
        await executor_lifecycle_module.execute_session_reply_work(work_id, WORKER_ID)

        assert len(provider_payloads) == 1
        provider_payload = provider_payloads[0]
        provider_items = provider_payload["messages"] if protocol == "OPENAI" else provider_payload["input"]
        provider_dump = json.dumps(provider_payload, ensure_ascii=False)
        assert "submission_context" not in provider_dump
        assert "obsolete-snapshot-sentinel" not in provider_dump
        assert "future-user-sentinel" not in provider_dump
        assert "future-assistant-sentinel" not in provider_dump
        assert "original terminal task" in provider_dump
        assert "stop the running process and do not rerun it" in provider_dump
        assert "Understood, I will stop and not rerun it." in provider_dump

        def decode_user_payload(item: dict):
            if item.get("role") != "user" or not isinstance(item.get("content"), str):
                return None
            try:
                envelope = json.loads(item["content"])
            except json.JSONDecodeError:
                return None
            if not isinstance(envelope, dict):
                return None
            user_message = envelope.get("user_message")
            if not isinstance(user_message, str):
                return user_message
            try:
                return json.loads(user_message)
            except json.JSONDecodeError:
                return user_message

        user_payloads = [decode_user_payload(item) for item in provider_items]
        assert "original terminal task" in user_payloads
        assert "stop the running process and do not rerun it" in user_payloads
        background_payloads = [item for item in user_payloads if isinstance(item, dict) and item.get("type") == "background_tool_result"]
        assert len(background_payloads) == 1
        background_payload = background_payloads[0]
        assert background_payload["task_id"] == task_id
        assert background_payload["tool_name"] == TERMINAL_WATCH_TOOL_NAME
        assert background_payload["source"] == "terminal_auto_watch"
        assert background_payload["instruction"] == EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT
        assert background_payload["request_context"] == []
        assert background_payload["purpose"] is None
        assert background_payload["task"] == expected_task_result
        assert background_payload["task"]["content"] == observed_result
        assert background_payload["tool_call"] == {
            "id": watch_tool_call_id,
            "name": TERMINAL_WATCH_TOOL_NAME,
            "arguments": {
                "terminal_session_id": terminal_session_id,
                "after_offset": 0,
                "condition": "process_exit",
            },
        }
        assert background_payload["tool_result"]["tool_call_id"] == background_payload["tool_call"]["id"]
        assert json.loads(background_payload["tool_result"]["content"]) == expected_task_result
        assert background_payload["task"]["content"]["terminal_session_id"] == terminal_session_id
        assert background_payload["task"]["content"]["event"] == event
        assert background_payload["task"]["content"]["session_status"] == session_status
        assert background_payload["task"]["content"]["exit_code"] == exit_code

        if protocol == "OPENAI":
            real_calls = [call["id"] for item in provider_items if item.get("role") == "assistant" for call in item.get("tool_calls", [])]
            real_results = [item.get("tool_call_id") for item in provider_items if item.get("role") == "tool"]
            assert real_calls == ["real-call"]
            assert real_results == ["real-call"]
            assert not any((item.get("role") == "tool" and str(item.get("tool_call_id", "")).startswith("terminal-auto-watch:")) or any(str(call.get("id", "")).startswith("terminal-auto-watch:") for call in item.get("tool_calls") or []) for item in provider_items)
        else:
            real_calls = [item.get("call_id") for item in provider_items if item.get("type") == "function_call"]
            real_results = [item.get("call_id") for item in provider_items if item.get("type") == "function_call_output"]
            assert real_calls == ["real-call"]
            assert real_results == ["real-call"]
            assert not any(item.get("type") in {"function_call", "function_call_output"} and str(item.get("call_id", "")).startswith("terminal-auto-watch:") for item in provider_items)

        async with factory() as db:
            final_work = await db.get(SessionReplyWorkItem, work_id)
            persisted_task = await db.get(BackgroundTask, task_id)
            assert final_work is not None
            assert final_work.status == SessionReplyWorkStatus.SUCCEEDED
            assert final_work.result_message_id is not None
            assert persisted_task is not None
            assert persisted_task.status == BackgroundTaskStatus.SUCCEEDED
            assert persisted_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
            assert persisted_task.result == expected_task_result
            assert persisted_task.extra.get("auto_terminal_watch") is True
            if legacy_snapshot == "missing":
                assert "submission_context" not in persisted_task.extra
            elif legacy_snapshot == "empty":
                assert persisted_task.extra.get("submission_context") == []
            else:
                assert persisted_task.extra["submission_context"] == [{"role": "user", "content": "obsolete-snapshot-sentinel"}]

            persisted_messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
            persisted_result_events = [message for message in persisted_messages if message.type == MessageType.BACKGROUND_TASK_RESULT]
            assert len(persisted_result_events) == 1
            assert persisted_result_events[0].id == result_event_id
            assert persisted_result_events[0].role == MessageRole.USER
            persisted_tool_results = [message for message in persisted_messages if message.type == MessageType.TOOL_RESULT]
            assert len(persisted_tool_results) == 1
            assert persisted_tool_results[0].content == real_tool_result_json
            persisted_real_result = InternalMessage.model_validate_json(persisted_tool_results[0].content or "{}")
            assert persisted_real_result.tool_call_id == "real-call"
            assert persisted_real_result.content == real_tool_result.content
            assert persisted_real_result.tool_execution_status == "succeeded"
            assert not any(InternalMessage.model_validate_json(message.content or "{}").tool_call_id == persisted_task.tool_call_id for message in persisted_tool_results)
            result_message = await db.get(Message, final_work.result_message_id)
            assert result_message is not None
            assert result_message.role == MessageRole.ASSISTANT
            assert result_message.type == MessageType.TEXT
            assert result_message.content == "terminal observation acknowledged"
            assert list((await db.execute(select(MessagePlatformOutbox))).scalars().all()) == []
