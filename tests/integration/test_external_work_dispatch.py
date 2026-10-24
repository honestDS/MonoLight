import json

import pytest
from sqlmodel import select

import app.core.background_tasks.external_work as external_work_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.message_platforms.notifier as notifier_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.providers.database as database_provider
from app.core.background_tasks.external_work import resume_external_work_for_task
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.message_platform_outbox import MessagePlatformOutbox
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.user import User
from app.transformers.openai import OpenAIChatCompletionsTransformer

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
            assert json.loads(tool_result["content"]) == {"stdout": "fixture", "exit_code": 0}

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
