import copy
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
import app.core.session_reply_queue.executor_replies as executor_replies_module
import app.core.tools.terminal as terminal_tool_module
import app.providers.database as database_provider
from app.core.background_tasks.external_work import resume_external_work_for_task
from app.core.background_tasks.reply_trigger import trigger_background_task_reply
from app.core.background_tasks.terminal_watch_coordinator import complete_terminal_watch_round
from app.core.constants import (
    END_SESSION_TOOL_NAME,
    MANAGE_BACKGROUND_TASKS_TOOL_NAME,
    MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
    MANAGE_TODO_TOOL_NAME,
    SHELL_TOOL_NAME,
    TERMINAL_TOOL_NAME,
    TERMINAL_WATCH_TOOL_NAME,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.exceptions import ResourceNotFoundException
from app.core.prompts import (
    BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT,
    TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT,
)
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.core.tools.terminal_watch import ensure_terminal_auto_watch
from app.core.utils.background_task_result import build_background_task_success_result
from app.core.utils.time import get_local_time
from app.models.background_task import BackgroundTask, BackgroundTaskReplyMode, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.message_platform_outbox import MessagePlatformOutbox
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession
from app.models.user import User
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer

UID = "external-dispatch-user"
SESSION_ID = "external-dispatch-session"
PROFILE_ID = 1
MODEL_ID = "external-dispatch-model"
WORKER_ID = "external-dispatch-worker"
TASK_WORKER_ID = "external-task-worker"


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
            db.add_all(
                [
                    channel,
                    profile,
                    user,
                    session,
                    TerminalSession(
                        terminal_session_id=terminal_session_id,
                        uid=UID,
                        session_id=SESSION_ID,
                        original_tool_call_id="real-call",
                        profile_id=PROFILE_ID,
                        command="test terminal command",
                        working_directory=".",
                        status=TerminalSessionStatus.RUNNING,
                        allowed_actions=[action.value for action in TerminalAction],
                    ),
                ]
            )
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
            assert await complete_terminal_watch_round(
                db,
                task=claimed_task,
                worker_id=TASK_WORKER_ID,
                content=observed_result,
            )

            completed_auto_watch_tasks = list(
                (
                    await db.execute(
                        select(BackgroundTask)
                        .where(
                            BackgroundTask.session_id == SESSION_ID,
                            BackgroundTask.tool_name == TERMINAL_WATCH_TOOL_NAME,
                        )
                        .order_by(BackgroundTask.id)
                    )
                )
                .scalars()
                .all()
            )
            completed_auto_watch_tasks = [task for task in completed_auto_watch_tasks if task.extra.get("auto_terminal_watch") is True]
            if event == "timeout":
                assert len(completed_auto_watch_tasks) == 2
                next_task = next(task for task in completed_auto_watch_tasks if task.id != task_id)
                assert next_task.status == BackgroundTaskStatus.PENDING
                assert next_task.reply_status == BackgroundTaskReplyStatus.PENDING
                assert next_task.arguments["after_offset"] == observed_result["next_offset"]
            else:
                assert event == "process_exit"
                assert len(completed_auto_watch_tasks) == 1

        result_event_id: int | None = None
        future_assistant_id: int | None = None
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
            assert len(result_events) == 0

            persisted_task = await db.get(BackgroundTask, task_id)
            assert persisted_task is not None
            assert persisted_task.status == BackgroundTaskStatus.SUCCEEDED
            assert persisted_task.result == expected_task_result

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
            summary_state = dict(summary_work.execution_state or {})
            external_resume_context = summary_state.get("external_resume_context")
            assert isinstance(external_resume_context, list)
            assert len(external_resume_context) == 1
            assert external_resume_context[0]["task_id"] == task_id
            assert external_resume_context[0]["task"] == expected_task_result
            deferred_checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(db, summary_work.id) or {})
            assert deferred_checkpoint.get("deferred_external_results") is True

            future_user = Message(
                session_id=SESSION_ID,
                uid=UID,
                profile_id=PROFILE_ID,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content="future-user-sentinel",
                is_processed=False,
            )
            future_assistant = Message(
                session_id=SESSION_ID,
                uid=UID,
                profile_id=PROFILE_ID,
                role=MessageRole.ASSISTANT,
                type=MessageType.TEXT,
                content="future-assistant-sentinel",
                is_processed=True,
            )
            db.add_all([future_user, future_assistant])
            await db.commit()
            assert future_user.id is not None
            assert future_assistant.id is not None
            assert future_user.id < future_assistant.id
            future_assistant_id = future_assistant.id

        provider_payloads: list[dict] = []

        async def fake_post_json(_transformer, **kwargs):
            nonlocal result_event_id

            payload = kwargs["payload"]
            provider_payloads.append(copy.deepcopy(payload))
            assert payload["model"] == MODEL_ID
            assert payload["stream"] is False
            assert kwargs["url"].endswith("/chat/completions" if protocol == "OPENAI" else "/responses")
            provider_items = payload["messages"] if protocol == "OPENAI" else payload["input"]
            assert provider_items[-1]["role"] == "user"
            assert future_assistant_id is not None
            async with factory() as inspection_db:
                persisted_result_events = list(
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
                assert len(persisted_result_events) == 1
                result_event = persisted_result_events[0]
                assert result_event.role == MessageRole.USER
                assert result_event.id is not None
                assert result_event.id > future_assistant_id
                result_event_id = result_event.id

                persisted_work = await inspection_db.get(SessionReplyWorkItem, work_id)
                assert persisted_work is not None
                checkpoint = dict(await session_reply_work_item_crud.get_checkpoint(inspection_db, persisted_work.id) or {})
                assert "reload_history" not in checkpoint
                assert "deferred_external_results" not in checkpoint
                checkpoint_messages = checkpoint.get("messages")
                assert isinstance(checkpoint_messages, list)
                checkpoint_message_ids = [item.get("id") for item in checkpoint_messages if isinstance(item, dict)]
                assert result_event_id in checkpoint_message_ids
                assert checkpoint_message_ids[-1] == result_event_id
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
        assert result_event_id is not None
        provider_payload = provider_payloads[0]
        provider_items = provider_payload["messages"] if protocol == "OPENAI" else provider_payload["input"]
        assert provider_items[-1]["role"] == "user"
        provider_dump = json.dumps(provider_payload, ensure_ascii=False)
        assert "submission_context" not in provider_dump
        assert "obsolete-snapshot-sentinel" not in provider_dump
        assert "future-user-sentinel" not in provider_dump
        assert "future-assistant-sentinel" not in provider_dump
        assert "original terminal task" in provider_dump
        assert "stop the running process and do not rerun it" not in provider_dump
        assert "Understood, I will stop and not rerun it." not in provider_dump

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
        assert "stop the running process and do not rerun it" not in user_payloads
        background_payloads = [item for item in user_payloads if isinstance(item, dict) and item.get("type") == "background_tool_result"]
        assert len(background_payloads) == 1
        background_payload = background_payloads[0]
        assert background_payload["task_id"] == task_id
        assert background_payload["tool_name"] == TERMINAL_WATCH_TOOL_NAME
        assert background_payload["source"] == "terminal_auto_watch"
        assert background_payload["instruction"] == TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT
        assert background_payload["request_context"] == [{"id": 1, "role": "user", "content": "original terminal task"}]
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

            auto_watch_tasks = [
                task
                for task in (
                    await db.execute(
                        select(BackgroundTask)
                        .where(
                            BackgroundTask.session_id == SESSION_ID,
                            BackgroundTask.tool_name == TERMINAL_WATCH_TOOL_NAME,
                        )
                        .order_by(BackgroundTask.id)
                    )
                )
                .scalars()
                .all()
                if task.extra.get("auto_terminal_watch") is True
            ]
            if event == "timeout":
                assert len(auto_watch_tasks) == 2
                next_task = next(task for task in auto_watch_tasks if task.id != task_id)
                assert next_task.status == BackgroundTaskStatus.PENDING
                assert next_task.reply_status == BackgroundTaskReplyStatus.PENDING
                assert next_task.arguments["terminal_session_id"] == terminal_session_id
                assert next_task.arguments["after_offset"] == observed_result["next_offset"]
                assert next_task.arguments["condition"] == "process_exit"
                assert persisted_task.extra["next_auto_watch_task_id"] == next_task.id
            else:
                assert event == "process_exit"
                assert len(auto_watch_tasks) == 1
                assert "next_auto_watch_task_id" not in persisted_task.extra

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


@pytest.mark.asyncio
async def test_ordinary_background_task_queues_tool_summary_without_tools(database_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    async with database_factory(foreign_keys=True, wal=True, name="background-summary-split.sqlite3") as factory:
        async with factory() as db:
            db.add_all(
                [
                    User(uid=UID, username="background-summary-user"),
                    Profile(id=PROFILE_ID, uid=UID, name="background-summary-profile", configs=ProfileConfig.model_validate({}).model_dump(mode="json")),
                    ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID, source="http"),
                ]
            )
            await db.commit()
            task = await background_task_crud.create_task(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile_id=PROFILE_ID,
                tool_call_id="background-summary-call",
                tool_name=SHELL_TOOL_NAME,
                arguments={"command": "echo summary"},
                auto_reply=True,
                extra={"source": "integration-seed", "reply_mode": BackgroundTaskReplyMode.SUMMARY.value},
            )
            assert task.id is not None
            task_id = task.id
            claimed = await background_task_crud.try_claim(db, task_id=task_id, worker_id=TASK_WORKER_ID, lease_seconds=300)
            assert claimed is not None
            assert await background_task_crud.mark_succeeded(
                db,
                task_id=task_id,
                worker_id=TASK_WORKER_ID,
                result=build_background_task_success_result(SHELL_TOOL_NAME, {"stdout": "summary", "stderr": "", "exit_code": 0}),
                auto_reply=True,
            )

        monkeypatch.setattr(reply_trigger_module, "AsyncSessionLocal", factory)
        await trigger_background_task_reply(task_id)

        async with factory() as db:
            work = (await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.source_id == str(task_id)))).scalars().one()
            assert work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY
            assert "external_resume_context" not in (work.execution_state or {})
            assert await session_reply_work_item_crud.get_checkpoint(db, work.id) is None
            captured: dict[str, object] = {}

            async def generate_auxiliary(_db, **kwargs):
                captured.update(kwargs)
                return InternalMessage(role=MessageRole.ASSISTANT, content="summary reply"), [], [], {"request_purpose": "background_summary"}

            monkeypatch.setattr(executor_replies_module, "_generate_auxiliary_reply_with_request_metadata", generate_auxiliary)
            response = await executor_replies_module._execute_background(db, work, "summary-worker")

        assert response["content"] == "summary reply"
        assert captured["allow_tools"] is False
        assert captured["submission_context"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"])
@pytest.mark.parametrize("source_auto", [True], ids=["automatic-watch"])
async def test_background_task_resume_runs_full_goal_tool_loop_after_external_watch(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    source_auto: bool,
) -> None:
    terminal_session_id = "background-chain-terminal-0123456789abcdef"
    assert len(terminal_session_id) >= 32
    observed_result = {
        "terminal_session_id": terminal_session_id,
        "event": "observation_error",
        "after_offset": 0,
        "session_status": "running",
        "exit_code": None,
        "failure_reason": None,
        "error": "terminal output read timed out",
        "output": "",
        "start_offset": 0,
        "next_offset": 0,
        "latest_offset": 0,
        "output_truncated": False,
        "eof": False,
    }
    auto_watch_result = dict(observed_result)
    expected_tools = {
        END_SESSION_TOOL_NAME,
        MANAGE_TODO_TOOL_NAME,
        MANAGE_BACKGROUND_TASKS_TOOL_NAME,
        MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
        SHELL_TOOL_NAME,
        TERMINAL_TOOL_NAME,
        TERMINAL_WATCH_TOOL_NAME,
    }
    provider_payloads: list[dict] = []
    terminal_wait_calls: list[tuple[int, float]] = []
    resources_released = False
    original_get_owned_session = terminal_tool_module.terminal_session_manager.get_owned_session
    tool_rounds = [
        (MANAGE_TODO_TOOL_NAME, {"operation": "read"}, "chain-todo-read"),
        (
            TERMINAL_WATCH_TOOL_NAME,
            {
                "terminal_session_id": terminal_session_id,
                "after_offset": 0,
                "condition": "process_exit",
            },
            "chain-terminal-watch",
        ),
        (
            TERMINAL_TOOL_NAME,
            {"terminal_session_id": terminal_session_id, "force": True, "operation": "close"},
            "chain-terminal-close",
        ),
        (END_SESSION_TOOL_NAME, {}, "chain-end-session"),
    ]

    def provider_tool_names(payload: dict) -> set[str]:
        names = set()
        for tool in payload.get("tools") or []:
            if protocol == "OPENAI":
                name = tool.get("function", {}).get("name")
            else:
                name = tool.get("name")
            if isinstance(name, str):
                names.add(name)
        return names

    def provider_response(request_number: int, call: tuple[str, dict, str] | None = None) -> dict:
        if call is None:
            content = [{"type": "output_text", "text": "background chain completed"}]
            output = [{"type": "message", "role": "assistant", "content": content}]
            message = {"role": "assistant", "content": "background chain completed"}
            finish_reason = "stop"
        else:
            call_name, arguments, call_id = call
            function_arguments = json.dumps(arguments, ensure_ascii=False)
            output = [{"type": "function_call", "id": call_id, "call_id": call_id, "name": call_name, "arguments": function_arguments, "status": "completed"}]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": call_name, "arguments": function_arguments},
                    }
                ],
            }
            finish_reason = "tool_calls"
        if protocol == "OPENAI":
            return {
                "id": f"chatcmpl-background-chain-{request_number}",
                "object": "chat.completion",
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        return {
            "id": f"resp-background-chain-{request_number}",
            "object": "response",
            "model": MODEL_ID,
            "status": "completed",
            "output": output,
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        }

    async def fake_post_json(_transformer, **kwargs):
        payload = kwargs["payload"]
        provider_payloads.append(payload)
        request_number = len(provider_payloads) - 1
        assert payload["model"] == MODEL_ID
        assert payload["stream"] is False
        assert kwargs["url"].endswith("/chat/completions" if protocol == "OPENAI" else "/responses")
        if request_number < len(tool_rounds):
            assert expected_tools.issubset(provider_tool_names(payload))
            assert BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT not in json.dumps(payload, ensure_ascii=False)
            call_name, arguments, call_id = tool_rounds[request_number]
            return provider_response(request_number, (call_name, arguments, call_id))
        assert request_number == len(tool_rounds)
        return provider_response(request_number)

    async def fake_wait_for_command_result(
        db,
        command_id: int,
        timeout_seconds: float,
        terminal_session_id: str | None = None,
    ) -> dict:
        nonlocal resources_released
        terminal_wait_calls.append((command_id, timeout_seconds))
        command = await db.get(TerminalControlCommand, command_id)
        assert command is not None
        assert TerminalAction(command.action) is TerminalAction.CLOSE
        assert command.status == TerminalControlCommandStatus.PENDING
        terminal_session = await db.get(TerminalSession, command.terminal_session_id)
        assert terminal_session is not None
        assert terminal_session.status is TerminalSessionStatus.RUNNING
        now = get_local_time()
        next_offset = terminal_session.next_output_offset
        close_result = {
            "status": TerminalSessionStatus.EXITED.value,
            "exit_code": 0,
            "failure_reason": None,
            "output": "",
            "start_offset": next_offset,
            "next_offset": next_offset,
            "latest_offset": next_offset,
            "output_truncated": next_offset > 0,
        }
        command.status = TerminalControlCommandStatus.SUCCEEDED
        command.result = close_result
        command.updated_at = now
        command.finished_at = now
        terminal_session.status = TerminalSessionStatus.EXITED
        terminal_session.exit_code = 0
        terminal_session.updated_at = now
        terminal_session.finished_at = now
        db.add_all([command, terminal_session])
        await db.commit()
        resources_released = True
        return {
            **close_result,
            "_terminal_snapshot": terminal_tool_module.terminal_session_manager._build_terminal_snapshot(terminal_session).model_dump(mode="json"),
        }

    async def fake_get_owned_session(db, requested_terminal_session_id, uid, session_id, *, lock: bool = False):
        if resources_released and requested_terminal_session_id == terminal_session_id:
            raise ResourceNotFoundException()
        return await original_get_owned_session(db, requested_terminal_session_id, uid, session_id, lock=lock)

    async with database_factory(
        foreign_keys=True,
        wal=True,
        name=f"background-chain-{protocol.lower()}-{int(source_auto)}.sqlite3",
    ) as factory:
        channel = ModelChannel(
            id=1,
            name="background-chain-channel",
            api_key="enc:v1:stored-test-key",
            base_url="https://llm.invalid",
            model_ids=[
                {
                    "model_id": MODEL_ID,
                    "usage": "CHAT",
                    "protocol": protocol,
                    "context_window_k": 32,
                    "max_tokens": 512,
                    "temperature": 0,
                    "top_p": 1,
                }
            ],
        )
        profile = Profile(
            id=PROFILE_ID,
            uid=UID,
            name="background-chain-profile",
            configs=ProfileConfig.model_validate(
                {
                    "channel": {
                        "chat_channel": {
                            "chat_timeout": 60,
                            "rules": [
                                {
                                    "channel_id": 1,
                                    "model_id": MODEL_ID,
                                    "priority": 1,
                                    "weight": 1,
                                }
                            ],
                        }
                    },
                    "security": {"audit_threshold": 0},
                    "tool": {"enabled_tools": [SHELL_TOOL_NAME, MANAGE_BACKGROUND_TASKS_TOOL_NAME]},
                    "other": {},
                    "memory": {"enabled": True, "precheck_enabled": True},
                }
            ).model_dump(mode="json"),
        )
        user = User(uid=UID, username="background-chain-user")
        session = ChatSession(
            session_id=SESSION_ID,
            uid=UID,
            profile_id=PROFILE_ID,
            source="http",
            goal_mode=True,
            max_turns=2,
            stream_llm_request=False,
            show_tool_calls=True,
        )
        terminal_session = TerminalSession(
            terminal_session_id=terminal_session_id,
            uid=UID,
            session_id=SESSION_ID,
            original_tool_call_id="initial-terminal-call",
            profile_id=PROFILE_ID,
            command="long-running authorized command",
            working_directory=".",
            status=TerminalSessionStatus.RUNNING,
            allowed_actions=[action.value for action in TerminalAction],
        )

        async with factory() as db:
            db.add_all([channel, profile, user, session])
            await db.flush()
            db.add(terminal_session)
            await db.flush()
            db.add(
                Message(
                    session_id=SESSION_ID,
                    uid=UID,
                    profile_id=PROFILE_ID,
                    role=MessageRole.USER,
                    type=MessageType.TEXT,
                    content="I authorize the terminal operation.",
                    is_processed=True,
                )
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
        monkeypatch.setattr(OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer, "_post_json", fake_post_json)
        monkeypatch.setattr(terminal_tool_module.terminal_session_manager, "wait_for_command_result", fake_wait_for_command_result)
        monkeypatch.setattr(terminal_tool_module.terminal_session_manager, "get_owned_session", fake_get_owned_session)

        async def noop_transport(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", noop_transport)
        monkeypatch.setattr(notifier_module, "send_session_event", noop_transport)

        if source_auto:
            seed_tool_name = TERMINAL_WATCH_TOOL_NAME
            seed_arguments = {
                "terminal_session_id": terminal_session_id,
                "after_offset": 0,
                "condition": "process_exit",
            }
        else:
            seed_tool_name = SHELL_TOOL_NAME
            seed_arguments = {
                "command": "echo initial background result",
                "execution_mode": "non_interactive",
            }
        seed_result = build_background_task_success_result(
            seed_tool_name,
            auto_watch_result if source_auto else {"stdout": "initial background result", "stderr": "", "exit_code": 0},
        )

        async with factory() as db:
            if source_auto:
                await ensure_terminal_auto_watch(
                    db,
                    uid=UID,
                    session_id=SESSION_ID,
                    profile_id=PROFILE_ID,
                    terminal_session_id=terminal_session_id,
                    after_offset=0,
                    session_status=TerminalSessionStatus.RUNNING,
                )
                seed_task = await background_task_crud.get_active_terminal_watch(
                    db,
                    UID,
                    SESSION_ID,
                    terminal_session_id,
                    automatic_only=True,
                )
                assert seed_task is not None
                assert seed_task.extra["auto_terminal_watch"] is True
                assert seed_task.extra["terminal_session_id"] == terminal_session_id
            else:
                seed_task = await background_task_crud.create_task(
                    db,
                    uid=UID,
                    session_id=SESSION_ID,
                    profile_id=PROFILE_ID,
                    tool_call_id="initial-execute-shell-call",
                    tool_name=SHELL_TOOL_NAME,
                    arguments=seed_arguments,
                    auto_reply=True,
                    extra={"source": "integration-seed"},
                )
            assert seed_task.id is not None
            seed_task_id = seed_task.id
            claimed_seed = await background_task_crud.try_claim(
                db,
                task_id=seed_task_id,
                worker_id=TASK_WORKER_ID,
                lease_seconds=300,
            )
            assert claimed_seed is not None
            assert await background_task_crud.mark_succeeded(
                db,
                task_id=seed_task_id,
                worker_id=TASK_WORKER_ID,
                result=seed_result,
                auto_reply=True,
            )

        await trigger_background_task_reply(seed_task_id)

        async with factory() as db:
            initial_work_items = list((await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.source_id == str(seed_task_id)).order_by(SessionReplyWorkItem.id))).scalars().all())
            assert len(initial_work_items) == 1
            work_id = initial_work_items[0].id
            assert work_id is not None
            claimed_work = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=WORKER_ID,
                lease_seconds=300,
            )
            assert claimed_work is not None
            assert claimed_work.id == work_id

        await executor_lifecycle_module.execute_session_reply_work(work_id, WORKER_ID)

        async with factory() as db:
            waiting_work = await db.get(SessionReplyWorkItem, work_id)
            assert waiting_work is not None
            assert waiting_work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
            assert waiting_work.result_message_id is None
            assert len(provider_payloads) == 2
            assert not any(message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT for message in (await db.execute(select(Message).where(Message.session_id == SESSION_ID))).scalars().all())
            tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
            followup_candidates = [task for task in tasks if task.id != seed_task_id and task.tool_name == TERMINAL_WATCH_TOOL_NAME and isinstance(task.extra, dict) and task.extra.get("terminal_watch_subscription") is True and task.extra.get("origin_work_id") == work_id]
            assert len(followup_candidates) == 1
            followup_task = followup_candidates[0]
            assert followup_task.id is not None
            followup_subscription_id = followup_task.id
            assert followup_task.auto_reply is False
            assert followup_task.status == BackgroundTaskStatus.PENDING
            assert followup_task.reply_status == BackgroundTaskReplyStatus.NONE
            assert followup_task.extra["terminal_watch_subscription"] is True
            assert followup_task.extra["origin_work_identity"] == waiting_work.dedupe_key
            assert followup_task.arguments == {
                "terminal_session_id": terminal_session_id,
                "after_offset": 0,
                "condition": "process_exit",
            }

            automatic_candidates = [
                task
                for task in tasks
                if task.id != seed_task_id
                and task.tool_name == TERMINAL_WATCH_TOOL_NAME
                and isinstance(task.extra, dict)
                and task.extra.get("auto_terminal_watch") is True
                and task.extra.get("origin_work_id") == work_id
                and task.extra.get("origin_work_identity") == waiting_work.dedupe_key
                and task.status == BackgroundTaskStatus.PENDING
            ]
            assert len(automatic_candidates) == 1
            automatic_task = automatic_candidates[0]
            assert automatic_task.id is not None
            automatic_task_id = automatic_task.id
            assert automatic_task.auto_reply is True
            assert automatic_task.reply_status == BackgroundTaskReplyStatus.PENDING
            observer_tasks = await background_task_crud.list_pending(
                db,
                profile_id=PROFILE_ID,
                observer=True,
            )
            assert [task.id for task in observer_tasks] == [automatic_task_id]
            assert followup_subscription_id not in {task.id for task in observer_tasks}

        followup_content = {
            **observed_result,
            "condition": "process_exit",
            "matched": False,
        }
        followup_result = build_background_task_success_result(TERMINAL_WATCH_TOOL_NAME, followup_content)
        automatic_result = build_background_task_success_result(
            TERMINAL_WATCH_TOOL_NAME,
            {**observed_result, "completed_watch_task_ids": [followup_subscription_id]},
        )
        async with factory() as db:
            claimed_automatic = await background_task_crud.try_claim(
                db,
                task_id=automatic_task_id,
                worker_id=TASK_WORKER_ID,
                lease_seconds=300,
            )
            assert claimed_automatic is not None
            assert await complete_terminal_watch_round(
                db,
                task=claimed_automatic,
                worker_id=TASK_WORKER_ID,
                content=observed_result,
            )
            completed_subscription = await db.get(BackgroundTask, followup_subscription_id)
            assert completed_subscription is not None
            assert completed_subscription.status == BackgroundTaskStatus.SUCCEEDED
            assert completed_subscription.reply_status == BackgroundTaskReplyStatus.NONE
            assert completed_subscription.attempt_count == 0
            assert completed_subscription.result == followup_result

        assert await resume_external_work_for_task(followup_subscription_id) is True

        async with factory() as db:
            resumed_work = await db.get(SessionReplyWorkItem, work_id)
            assert resumed_work is not None
            assert resumed_work.status == SessionReplyWorkStatus.READY_FOR_LLM
            summary_work_items = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
            assert len(summary_work_items) == 1
            assert summary_work_items[0].id == work_id
            resumed_claim = await session_reply_work_item_crud.claim_next(
                db,
                worker_id=WORKER_ID,
                lease_seconds=300,
            )
            assert resumed_claim is not None
            assert resumed_claim.id == work_id

        await executor_lifecycle_module.execute_session_reply_work(work_id, WORKER_ID)

        assert len(provider_payloads) == 5
        assert len(terminal_wait_calls) == 1
        assert all(str(followup_subscription_id) in json.dumps(payload, ensure_ascii=False) for payload in provider_payloads[2:])

        async with factory() as db:
            final_work = await db.get(SessionReplyWorkItem, work_id)
            seed_task = await db.get(BackgroundTask, seed_task_id)
            followup_task = await db.get(BackgroundTask, followup_subscription_id)
            terminal_session = await db.get(TerminalSession, terminal_session_id)
            assert final_work is not None
            assert final_work.status == SessionReplyWorkStatus.SUCCEEDED
            assert final_work.result_message_id is not None
            assert seed_task is not None
            assert seed_task.status == BackgroundTaskStatus.SUCCEEDED
            assert seed_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
            assert seed_task.attempt_count == 1
            assert seed_task.result == seed_result
            assert followup_task is not None
            assert followup_task.status == BackgroundTaskStatus.SUCCEEDED
            assert followup_task.reply_status == BackgroundTaskReplyStatus.NONE
            assert followup_task.attempt_count == 0
            assert followup_task.result == followup_result
            assert terminal_session is not None
            assert terminal_session.status == TerminalSessionStatus.EXITED
            assert terminal_session.exit_code == 0

            persisted_tasks = list((await db.execute(select(BackgroundTask).order_by(BackgroundTask.id))).scalars().all())
            assert len(persisted_tasks) == 3
            assert len([task for task in persisted_tasks if task.id == seed_task_id]) == 1
            auto_watch_tasks = [task for task in persisted_tasks if task.extra.get("auto_terminal_watch") is True]
            assert len(auto_watch_tasks) == (2 if source_auto else 1)
            current_auto_task = await db.get(BackgroundTask, automatic_task_id)
            assert current_auto_task is not None
            assert current_auto_task.id != seed_task_id
            assert current_auto_task.status == BackgroundTaskStatus.SUCCEEDED
            assert current_auto_task.auto_reply is True
            assert current_auto_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
            assert current_auto_task.attempt_count == 1
            assert current_auto_task.result == automatic_result
            assert current_auto_task.result["content"]["event"] == "observation_error"
            assert current_auto_task.result["content"]["error"] == "terminal output read timed out"
            assert "next_auto_watch_task_id" not in current_auto_task.extra
            assert current_auto_task.extra["origin_work_id"] == work_id
            assert current_auto_task.extra["origin_work_identity"] == final_work.dedupe_key
            assert current_auto_task.extra["terminal_session_id"] == terminal_session_id
            assert current_auto_task.arguments == {
                "terminal_session_id": terminal_session_id,
                "after_offset": observed_result["next_offset"],
                "condition": "process_exit",
            }
            if source_auto:
                assert seed_task.id == seed_task_id
                assert seed_task.extra["auto_terminal_watch"] is True
                assert "next_auto_watch_task_id" not in seed_task.extra

            work_items = list((await db.execute(select(SessionReplyWorkItem).order_by(SessionReplyWorkItem.id))).scalars().all())
            assert len(work_items) == 1
            assert work_items[0].id == work_id

            commands = list((await db.execute(select(TerminalControlCommand).where(TerminalControlCommand.terminal_session_id == terminal_session_id))).scalars().all())
            assert len(commands) == 1
            assert TerminalAction(commands[0].action) is TerminalAction.CLOSE
            assert commands[0].status == TerminalControlCommandStatus.SUCCEEDED
            assert commands[0].result == {
                "status": TerminalSessionStatus.EXITED.value,
                "exit_code": 0,
                "failure_reason": None,
                "output": "",
                "start_offset": 0,
                "next_offset": 0,
                "latest_offset": 0,
                "output_truncated": False,
            }

            persisted_messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
            control_messages = [message for message in persisted_messages if isinstance(message.provider_metadata, dict) and message.provider_metadata.get("internal_control") == END_SESSION_TOOL_NAME]
            assert len(control_messages) == 2
            control_call_messages = [message for message in control_messages if message.type == MessageType.TOOL_CALL]
            control_result_messages = [message for message in control_messages if message.type == MessageType.TOOL_RESULT]
            assert len(control_call_messages) == 1
            assert len(control_result_messages) == 1
            control_call = InternalMessage.model_validate_json(control_call_messages[0].content or "{}")
            assert control_call.tool_calls is not None
            assert len(control_call.tool_calls) == 1
            assert control_call.tool_calls[0].name == END_SESSION_TOOL_NAME
            assert control_call.tool_calls[0].arguments == {}
            control_result = InternalMessage.model_validate_json(control_result_messages[0].content or "{}")
            assert control_result.tool_call_id == control_call.tool_calls[0].id
            control_result_payload = json.loads(control_result.content or "{}")
            assert control_result_payload["status"] == "completed"
            assert control_result_payload["skip_reply"] is False
            assert control_call_messages[0].id < control_result_messages[0].id
            control_message_ids = {message.id for message in control_messages}
            non_control_messages = [message for message in persisted_messages if message.id not in control_message_ids]
            tool_call_messages = [message for message in non_control_messages if message.type == MessageType.TOOL_CALL]
            tool_calls = [InternalMessage.model_validate_json(message.content or "{}") for message in tool_call_messages]
            assert [tool_call.name for message in tool_calls for tool_call in message.tool_calls or []] == [
                MANAGE_TODO_TOOL_NAME,
                TERMINAL_WATCH_TOOL_NAME,
                TERMINAL_TOOL_NAME,
            ]
            tool_result_messages = [message for message in non_control_messages if message.type == MessageType.TOOL_RESULT]
            tool_results = [InternalMessage.model_validate_json(message.content or "{}") for message in tool_result_messages]
            assert {tool_result.tool_call_id for tool_result in tool_results} == {tool_call.id for message in tool_calls for tool_call in message.tool_calls or []}
            result_by_call_id = {tool_result.tool_call_id: tool_result for tool_result in tool_results}
            todo_result = json.loads(result_by_call_id[tool_calls[0].tool_calls[0].id].content or "{}")
            assert todo_result["status"] == "success"
            watch_result = json.loads(result_by_call_id[tool_calls[1].tool_calls[0].id].content or "{}")
            assert watch_result["status"] == "queued"
            assert watch_result["task_id"] == followup_subscription_id
            close_result = json.loads(result_by_call_id[tool_calls[2].tool_calls[0].id].content or "{}")
            assert close_result["action"] == TerminalAction.CLOSE.value
            assert close_result["session_status"] == TerminalSessionStatus.EXITED.value
            assert not any(END_SESSION_TOOL_NAME in (message.content or "") for message in [*tool_call_messages, *tool_result_messages])

            result_events = [message for message in persisted_messages if message.type == MessageType.BACKGROUND_TASK_RESULT]
            assert len(result_events) == 2
            initial_payload = json.loads(next(message for message in result_events if message.dedupe_key == f"background-task:{seed_task_id}:result").content or "{}")
            assert initial_payload["task_id"] == seed_task_id
            assert initial_payload["task"] == seed_result
            marker_message = next(message for message in result_events if message.dedupe_key == f"background-task:{followup_subscription_id}:result")
            marker_payload = json.loads(marker_message.content or "{}")
            assert marker_payload["task_id"] == followup_subscription_id
            assert marker_payload["tool_name"] == TERMINAL_WATCH_TOOL_NAME
            assert marker_payload["execution_status"] == "succeeded"
            assert marker_payload["task"] == followup_result["content"]
            assert marker_payload["tool_call"] == {
                "id": followup_task.tool_call_id,
                "name": TERMINAL_WATCH_TOOL_NAME,
                "arguments": followup_task.arguments,
            }
            assert marker_payload["original_tool_result_message_id"] == next(message.id for message in tool_result_messages if InternalMessage.model_validate_json(message.content or "{}").tool_call_id == tool_calls[1].tool_calls[0].id)

            result_message = await db.get(Message, final_work.result_message_id)
            assert result_message is not None
            assert result_message.role == MessageRole.ASSISTANT
            assert result_message.type == MessageType.TEXT
            assert result_message.content == "background chain completed"
            assistant_text_messages = [message for message in persisted_messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
            assert [message.id for message in assistant_text_messages] == [result_message.id]
