import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatcher as dispatcher_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.core.tools.send_file_to_user as send_file_to_user_module
from app.api.v1.chat import router
from app.core.background_tasks.manager import BackgroundTaskManager
from app.core.security import get_current_user
from app.core.utils.background_task_result import build_background_task_success_result
from app.core.utils.dispatcher.handle_parallel_tool_limit import handle_parallel_tool_limit
from app.core.utils.dispatcher.save_message import save_message
from app.handler import register_handlers
from app.models.background_task import (
    BackgroundTask,
    BackgroundTaskReplyStatus,
    BackgroundTaskStatus,
)
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from app.providers.database import get_db
from app.providers.llm.client import LLMClient
from app.transformers.openai import OpenAIResponsesTransformer

UID = "http-image-user"
SESSION_ID = "http-image-session"
MODEL_ID = "http-image-model"


async def _create_registered_tables(db_session: AsyncSession) -> None:
    connection = await db_session.connection()
    await connection.run_sync(SQLModel.metadata.create_all)


async def _seed_reply_work(
    db_session: AsyncSession,
    *,
    task_status: BackgroundTaskStatus,
    reply_status: BackgroundTaskReplyStatus,
) -> tuple[Profile, ChatSession, SessionReplyWorkItem, BackgroundTask]:
    profile = Profile(uid=UID, name="http image profile", configs={})
    db_session.add(profile)
    await db_session.flush()
    assert profile.id is not None

    session = ChatSession(
        session_id=SESSION_ID,
        uid=UID,
        profile_id=profile.id,
        source="http",
        show_tool_calls=False,
    )
    work = SessionReplyWorkItem(
        uid=UID,
        session_id=SESSION_ID,
        profile_id=profile.id,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="http-image-foreground-work",
        status=SessionReplyWorkStatus.SUCCEEDED,
        execution_state={
            "response": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "已提交生图",
                        },
                        "finish_reason": True,
                    }
                ],
                "history": [],
            }
        },
    )
    task = BackgroundTask(
        uid=UID,
        session_id=SESSION_ID,
        profile_id=profile.id,
        tool_call_id="generate-image-call",
        tool_name="generate_image",
        status=task_status,
        arguments={"prompt": "test apple"},
        auto_reply=True,
        reply_status=reply_status,
    )
    db_session.add_all([session, work, task])
    await db_session.commit()
    await db_session.refresh(work)
    await db_session.refresh(task)
    assert work.id is not None
    assert task.id is not None
    return profile, session, work, task


def _build_app(db_session: AsyncSession) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield db_session

    def override_get_current_user():
        return SimpleNamespace(uid=UID)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_status", "reply_status"),
    [
        (BackgroundTaskStatus.PENDING, BackgroundTaskReplyStatus.PENDING),
        (BackgroundTaskStatus.RUNNING, BackgroundTaskReplyStatus.PENDING),
        (BackgroundTaskStatus.SUCCEEDED, BackgroundTaskReplyStatus.RUNNING),
    ],
)
async def test_http_reply_work_status_exposes_hidden_background_image_activity(
    db_session: AsyncSession,
    task_status: BackgroundTaskStatus,
    reply_status: BackgroundTaskReplyStatus,
) -> None:
    await _create_registered_tables(db_session)
    _profile, _session, work, _task = await _seed_reply_work(
        db_session,
        task_status=task_status,
        reply_status=reply_status,
    )
    app = _build_app(db_session)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/v1/chat/reply-works/{work.id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
    assert data["response"]["history"] == []
    assert data["response"]["has_background_tasks"] is True
    assert data["response"]["background_task_poll_interval"] == 2


@pytest.mark.asyncio
async def test_completed_background_image_message_is_returned_by_http_history(
    database_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "completed-background-image.png"
    image = Image.new("RGB", (2, 2), color=(32, 128, 224))
    try:
        image.save(image_path, format="PNG")
    finally:
        image.close()
    image_size = image_path.stat().st_size

    async with database_factory(foreign_keys=True, wal=True) as factory:
        async with factory() as db:
            channel = ModelChannel(
                id=1,
                name="http-image-test-channel",
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
                uid=UID,
                name="http image workflow profile",
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
                        "tool": {
                            "enabled_tools": ["send_file_to_user"],
                            "allowed_operation_dirs": [str(tmp_path.resolve())],
                        },
                        "other": {},
                        "memory": {"enabled": False},
                    }
                ).model_dump(mode="json"),
            )
            user = User(uid=UID, username="http_image_user")
            db.add_all([channel, profile, user])
            await db.flush()
            assert profile.id is not None

            session = ChatSession(
                session_id=SESSION_ID,
                uid=UID,
                profile_id=profile.id,
                source="http",
                show_tool_calls=False,
            )
            db.add(session)
            await db.flush()

            task = BackgroundTask(
                uid=UID,
                session_id=SESSION_ID,
                profile_id=profile.id,
                tool_call_id="generate-image-call",
                tool_name="generate_image",
                status=BackgroundTaskStatus.SUCCEEDED,
                arguments={"prompt": "test apple"},
                result=build_background_task_success_result(
                    "generate_image",
                    {"path": str(image_path)},
                ),
                auto_reply=True,
                reply_status=BackgroundTaskReplyStatus.RUNNING,
                extra={"submission_context": []},
            )
            db.add(task)
            await db.flush()
            assert task.id is not None

            work = SessionReplyWorkItem(
                uid=UID,
                session_id=SESSION_ID,
                profile_id=profile.id,
                sequence_no=1,
                work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
                source_type=SessionReplySourceType.BACKGROUND_TASK,
                source_id=str(task.id),
                dedupe_key="http-image-background-work",
                status=SessionReplyWorkStatus.RUNNING,
                execution_state={},
                locked_by="image-worker",
                lock_until=int(time.time()) + 300,
                attempt_count=1,
            )
            db.add(work)
            await db.commit()
            await db.refresh(work)
            work_id = work.id
            task_id = task.id
            assert work_id is not None
            assert task_id is not None

        async def fake_generate(**kwargs):
            messages = kwargs["messages"]
            has_send_file_result = any(message.role == MessageRole.TOOL and message.tool_call_id == "send-file-call" for message in messages)
            if not has_send_file_result:
                background_result_message = next(message for message in messages if message.role == MessageRole.TOOL and message.tool_call_id == "generate-image-call")
                background_result = json.loads(background_result_message.content or "{}")
                assert background_result["status"] == "succeeded"
                assert background_result["tool_name"] == "generate_image"
                result_path = background_result["content"]["path"]
                message = InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="send-file-call",
                            name="send_file_to_user",
                            arguments={"files": [{"path": result_path}]},
                        )
                    ],
                )
            else:
                message = InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="后台图片已发送",
                )
            return InternalResponse(
                message=message,
                model=kwargs["model_id"],
                usage={},
            )

        async def noop_send_session_event(*_args, **_kwargs):
            return None

        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
        monkeypatch.setattr(LLMClient, "generate", fake_generate)
        monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(send_file_to_user_module, "_get_encryption_key", lambda: b"t" * 32)
        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", noop_send_session_event)

        await executor_lifecycle_module.execute_session_reply_work(work_id, "image-worker")

        async with factory() as db:
            persisted_work = await db.get(SessionReplyWorkItem, work_id)
            persisted_task = await db.get(BackgroundTask, task_id)
            assert persisted_work is not None
            assert persisted_task is not None
            assert persisted_work.status == SessionReplyWorkStatus.SUCCEEDED
            assert persisted_work.result_message_id is not None
            assert persisted_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
            result_message_id = persisted_work.result_message_id

        async def request_history() -> list[dict]:
            async with factory() as db:
                app = _build_app(db)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    pending_activity = await client.get(
                        "/api/v1/chat/background-tasks/pending-activity",
                        params={"session_id": SESSION_ID},
                    )
                    history = await client.get(
                        "/api/v1/chat/sessions/history",
                        params={"session_id": SESSION_ID},
                    )

            assert pending_activity.status_code == 200
            assert pending_activity.json()["data"] == {"has_pending_activity": False}
            assert history.status_code == 200
            return history.json()["data"]

        def find_assistant_file_items(history_items: list[dict]) -> list[tuple[dict, dict]]:
            matches = []
            for item in history_items:
                if item.get("role") != MessageRole.ASSISTANT.value or item.get("type") != MessageType.TEXT.value:
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    try:
                        content = json.loads(content)
                    except json.JSONDecodeError:
                        continue
                if isinstance(content, dict) and content.get("type") == "assistant_files":
                    matches.append((item, content))
            return matches

        history_items = await request_history()
        assistant_file_items = find_assistant_file_items(history_items)
        assert len(assistant_file_items) == 1
        history_item, assistant_files = assistant_file_items[0]
        files = assistant_files["files"]
        assert len(files) == 1
        file_payload = files[0]
        assert file_payload["mime_type"] == "image/png"
        assert file_payload["size"] == image_size
        assert send_file_to_user_module.resolve_file_token(file_payload["id"]) == image_path.resolve()
        assert history_item["id"] == result_message_id

        await executor_lifecycle_module.execute_session_reply_work(work_id, "image-worker")
        repeated_matches = find_assistant_file_items(await request_history())
        assert len(repeated_matches) == 1
        assert repeated_matches[0][0]["id"] == history_item["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "requested_count",
        "max_parallel_tools",
        "target_index",
        "sibling_indexes",
        "sibling_result_kind",
        "history_id_reuse",
    ),
    [
        (1, 5, 0, (), "none", "none"),
        (5, 5, 2, (), "none", "none"),
        (5, 5, 2, (0,), "queued", "none"),
        (8, 5, 2, (), "none", "none"),
        (8, 5, 2, (0, 4), "queued", "none"),
        (8, 5, 2, (4, 0), "queued", "none"),
        (5, 5, 2, (0,), "file_success", "none"),
        (5, 5, 2, (0,), "queued", "sibling"),
        (5, 5, 2, (0,), "queued", "target"),
    ],
)
async def test_background_image_summary_keeps_task_tool_results_paired(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    requested_count: int,
    max_parallel_tools: int,
    target_index: int,
    sibling_indexes: tuple[int, ...],
    sibling_result_kind: str,
    history_id_reuse: str,
) -> None:
    async with database_factory(foreign_keys=True, wal=True) as factory:
        async with factory() as db:
            channel = ModelChannel(
                id=1,
                name="background-image-context-channel",
                api_key="enc:v1:stored-test-key",
                base_url="https://llm.invalid",
                model_ids=[
                    {
                        "model_id": MODEL_ID,
                        "usage": "CHAT",
                        "protocol": "OPENAI_RESPONSES",
                        "context_window_k": 32,
                        "max_tokens": 512,
                    }
                ],
            )
            profile = Profile(
                uid=UID,
                name="background image context profile",
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
                        "tool": {
                            "max_parallel_tools": max_parallel_tools,
                            "background_task_max_concurrency": 2,
                            "enabled_tools": ["generate_image", "file_tool"],
                        },
                        "other": {},
                        "memory": {"enabled": False},
                    }
                ).model_dump(mode="json"),
            )
            user = User(uid=UID, username="background_image_context_user")
            db.add_all([channel, profile, user])
            await db.flush()
            assert profile.id is not None

            session = ChatSession(
                session_id=SESSION_ID,
                uid=UID,
                profile_id=profile.id,
                source="http",
                show_tool_calls=False,
            )
            db.add(session)
            await db.flush()

            history_calls = [
                InternalToolCall(
                    id="history-image-call",
                    name="generate_image",
                    arguments={"prompt": "history image"},
                ),
                InternalToolCall(
                    id="history-file-call",
                    name="file_tool",
                    arguments={"operation": "read", "path": "history.txt"},
                ),
            ]

            current_calls = []
            for index in range(requested_count):
                tool_name = "file_tool" if sibling_result_kind == "file_success" and index == 0 else "generate_image"
                arguments = {"operation": "read", "path": "report.txt"} if tool_name == "file_tool" else {"prompt": f"current image {index + 1}"}
                current_calls.append(
                    InternalToolCall(
                        id=f"image-call-{index + 1}",
                        name=tool_name,
                        arguments=arguments,
                    )
                )
            target_call = current_calls[target_index]
            if history_id_reuse == "sibling":
                history_calls[1].id = current_calls[0].id
            elif history_id_reuse == "target":
                history_calls[0].id = target_call.id
            history_results = [
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=history_calls[1].id,
                    content=json.dumps(
                        {"status": "success", "tool_name": "file_tool", "entries": ["history.txt"]},
                        ensure_ascii=False,
                    ),
                ),
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=history_calls[0].id,
                    content=json.dumps(
                        {"status": "success", "tool_name": "generate_image", "frame": "history"},
                        ensure_ascii=False,
                    ),
                ),
            ]
            history_assistant = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=history_calls)
            rejected_results = handle_parallel_tool_limit(current_calls, max_parallel_tools)[1]
            sibling_results = []
            for index in sibling_indexes:
                sibling_call = current_calls[index]
                if sibling_result_kind == "file_success":
                    sibling_content = {
                        "status": "success",
                        "tool_name": sibling_call.name,
                        "content": {"path": "report.txt"},
                    }
                else:
                    sibling_content = {
                        "status": "queued",
                        "tool_name": sibling_call.name,
                        "task_id": 100 + index,
                    }
                sibling_results.append(
                    InternalMessage(
                        role=MessageRole.TOOL,
                        tool_call_id=sibling_call.id,
                        content=json.dumps(sibling_content, ensure_ascii=False),
                    )
                )

            history_user = InternalMessage.from_user_input(content="此前用户文本")
            history_text = InternalMessage(role=MessageRole.ASSISTANT, content="此前助手文本")
            current_user = InternalMessage.from_user_input(content="当前用户文本")
            current_assistant = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=current_calls)
            current_results = [*rejected_results, *sibling_results]
            submission_messages = [
                history_user,
                history_assistant,
                *history_results,
                history_text,
                current_user,
                current_assistant,
                *current_results,
            ]

            original_message_contents = {}
            for message in submission_messages:
                message_type = MessageType.TOOL_RESULT if message.role == MessageRole.TOOL else MessageType.TOOL_CALL if message.tool_calls else MessageType.TEXT
                saved_message = await save_message(
                    db,
                    SESSION_ID,
                    UID,
                    message.role,
                    message_type,
                    message,
                    profile.id,
                )
                assert saved_message.id is not None
                message.id = saved_message.id
                original_message_contents[message.id] = saved_message.content

            submission_payload = [message.model_dump(mode="json", exclude_none=True) for message in submission_messages]
            task = await BackgroundTaskManager().submit(
                db,
                uid=UID,
                session_id=SESSION_ID,
                profile=profile,
                tool_call_id=target_call.id,
                tool_name="generate_image",
                arguments=target_call.arguments,
                messages=submission_messages,
            )
            assert task.id is not None
            task_id = task.id
            assert submission_payload == [message.model_dump(mode="json", exclude_none=True) for message in submission_messages]
            expected_submission_context = task.extra["submission_context"]
            assert isinstance(expected_submission_context, list)

        task_result = build_background_task_success_result(
            "generate_image",
            {"status": "success", "frame": target_index + 1},
        )
        worker_id = "background-image-context-worker"
        async with factory() as db:
            persisted_task = await db.get(BackgroundTask, task_id)
            assert persisted_task is not None
            persisted_task.status = BackgroundTaskStatus.SUCCEEDED
            persisted_task.result = task_result
            persisted_task.reply_status = BackgroundTaskReplyStatus.RUNNING
            persisted_task.reply_locked_by = worker_id
            persisted_task.reply_lock_until = int(time.time()) + 300
            work = SessionReplyWorkItem(
                uid=UID,
                session_id=SESSION_ID,
                profile_id=profile.id,
                sequence_no=1,
                work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
                source_type=SessionReplySourceType.BACKGROUND_TASK,
                source_id=str(task_id),
                dedupe_key="background-image-context-reply",
                status=SessionReplyWorkStatus.RUNNING,
                execution_state={},
                locked_by=worker_id,
                lock_until=int(time.time()) + 300,
                attempt_count=1,
            )
            db.add(work)
            await db.commit()
            await db.refresh(work)
            assert work.id is not None
            work_id = work.id

        expected_function_calls = [(call.id, call.arguments) for call in history_calls] + [(target_call.id, target_call.arguments)]
        expected_function_call_outputs = [(message.tool_call_id, json.loads(message.content or "{}")) for message in history_results] + [(target_call.id, task_result)]
        provider_payloads = []

        async def fake_post_json(_transformer, **kwargs):
            payload = kwargs["payload"]
            provider_payloads.append(payload)
            input_items = payload["input"]
            pending_call_ids = set()
            function_calls = []
            function_call_outputs = []
            for item in input_items:
                if item.get("type") == "function_call":
                    call_id = item["call_id"]
                    assert call_id not in pending_call_ids
                    pending_call_ids.add(call_id)
                    function_calls.append((call_id, json.loads(item["arguments"])))
                elif item.get("type") == "function_call_output":
                    call_id = item["call_id"]
                    assert call_id in pending_call_ids
                    pending_call_ids.remove(call_id)
                    function_call_outputs.append((call_id, json.loads(item["output"])))
                elif item.get("role") in {"user", "assistant"}:
                    assert not pending_call_ids
            assert not pending_call_ids
            assert function_calls == expected_function_calls
            assert function_call_outputs == expected_function_call_outputs
            user_messages = []
            for item in input_items:
                if item.get("role") != "user" or not isinstance(item.get("content"), str):
                    continue
                try:
                    user_payload = json.loads(item["content"])
                except json.JSONDecodeError:
                    continue
                if isinstance(user_payload, dict):
                    user_messages.append(user_payload.get("user_message"))
            assert "此前用户文本" in user_messages
            assert "当前用户文本" in user_messages
            assert any(item.get("role") == "assistant" and item.get("content") == "此前助手文本" for item in input_items)
            return {
                "status": "completed",
                "model": MODEL_ID,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "当前图片生成完成"}],
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            }

        events = []

        async def capture_send_session_event(uid: str, session_id: str, event: dict) -> None:
            events.append((uid, session_id, event))

        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
        monkeypatch.setattr(OpenAIResponsesTransformer, "_post_json", fake_post_json)
        monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_send_session_event)

        await executor_lifecycle_module.execute_session_reply_work(work_id, worker_id)

        assert len(provider_payloads) == 1
        assert len(events) == 1
        assert events[0][0:2] == (UID, SESSION_ID)
        assert events[0][2]["type"] == "proactive_reply"
        assert events[0][2]["content"] == "当前图片生成完成"
        assert events[0][2]["task_id"] == task_id

        async with factory() as db:
            persisted_work = await db.get(SessionReplyWorkItem, work_id)
            persisted_task = await db.get(BackgroundTask, task_id)
            assert persisted_work is not None
            assert persisted_task is not None
            assert persisted_work.status == SessionReplyWorkStatus.SUCCEEDED
            assert persisted_work.result_message_id is not None
            assert persisted_task.status == BackgroundTaskStatus.SUCCEEDED
            assert persisted_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
            assert persisted_task.result == task_result
            assert persisted_task.extra["submission_context"] == expected_submission_context

            result_message = await db.get(Message, persisted_work.result_message_id)
            assert result_message is not None
            assert result_message.role == MessageRole.ASSISTANT
            assert result_message.type == MessageType.TEXT
            assert result_message.content == "当前图片生成完成"
            for message_id, original_content in original_message_contents.items():
                original_message = await db.get(Message, message_id)
                assert original_message is not None
                assert original_message.content == original_content
