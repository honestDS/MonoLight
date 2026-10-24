import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel, select

import app.core.background_tasks.reply_trigger as reply_trigger_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.session_reply_queue.consumer as consumer_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.core.tools.send_file_to_user as send_file_to_user_module
from app.api.v1.chat import router
from app.core.background_tasks.manager import BackgroundTaskManager
from app.core.constants import ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED
from app.core.i18n import t
from app.core.prompts import (
    BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT,
    BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT,
    BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT,
)
from app.core.security import get_current_user
from app.core.tools.file_tool.read import read_file
from app.core.tools.image_generation import ImageGenerationExecutor
from app.core.utils.background_task_result import build_background_task_success_result
from app.core.utils.dispatcher.handle_parallel_tool_limit import handle_parallel_tool_limit
from app.core.utils.dispatcher.process_single_tool import _build_background_task_queued_result
from app.core.utils.dispatcher.save_message import save_message
from app.handler import register_handlers
from app.models.background_task import (
    BackgroundTask,
    BackgroundTaskReplyStatus,
    BackgroundTaskStatus,
)
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
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
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer
from tests.unit.response_test_support import llm_response_payload

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
        execution_state={"response": llm_response_payload("已提交生图", history=[])},
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
@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"])
@pytest.mark.parametrize("final_tool_violations", [0, 1, 3, 4])
async def test_completed_background_image_message_is_returned_by_http_history(
    database_factory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    final_tool_violations: int,
) -> None:
    image_path = tmp_path / "completed-background-image.png"
    image = Image.new("RGB", (2, 2), color=(32, 128, 224))
    try:
        image.save(image_path, format="PNG")
    finally:
        image.close()
    image_size = image_path.stat().st_size
    image_executor = ImageGenerationExecutor(tmp_path, UID)
    image_success_payload = json.loads(
        image_executor._build_success_payload(
            {
                "path": str(image_path),
                "name": image_path.name,
                "description": "Generated image",
                "mime_type": "image/png",
            }
        )
    )
    task_result = build_background_task_success_result(
        "generate_image",
        image_success_payload,
    )

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
                        "protocol": protocol,
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
                result=task_result,
                auto_reply=True,
                reply_status=BackgroundTaskReplyStatus.RUNNING,
                extra={"submission_context": []},
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            db.add(
                Message(
                    session_id=SESSION_ID,
                    uid=UID,
                    profile_id=profile.id,
                    role=MessageRole.USER,
                    type=MessageType.BACKGROUND_TASK_RESULT,
                    content=reply_trigger_module._build_background_tool_result_message(task),
                    is_processed=True,
                    dedupe_key=f"background-task:{task.id}:result",
                )
            )

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

        provider_payloads = []
        invalid_tool_call_ids = []

        def provider_items(payload: dict) -> list[dict]:
            return payload["messages"] if protocol == "OPENAI" else payload["input"]

        def find_guidance(items: list[dict]) -> tuple[int, dict]:
            user_items = [(index, item) for index, item in enumerate(items) if item.get("role") == MessageRole.USER.value and isinstance(item.get("content"), str)]
            assert user_items
            guidance_items = []
            for index, item in user_items:
                assert item["content"].strip()
                content = json.loads(item["content"])
                if isinstance(content, dict) and content.get("user_message") is None:
                    guidance_items.append((index, content))
            assert len(guidance_items) == 1
            guidance_index, guidance = guidance_items[0]
            assert guidance_index == user_items[-1][0] == len(items) - 1
            return guidance_index, guidance

        def find_tool_output(items: list[dict], call_id: str) -> dict:
            if protocol == "OPENAI":
                tool_item = next(item for item in items if item.get("role") == MessageRole.TOOL.value and item.get("tool_call_id") == call_id)
                content = tool_item["content"]
            else:
                tool_item = next(item for item in items if item.get("type") == "function_call_output" and item.get("call_id") == call_id)
                content = tool_item["output"]
            assert isinstance(content, str)
            return json.loads(content)

        def find_background_result(items: list[dict]) -> dict:
            background_results = []
            for item in items:
                if item.get("role") != MessageRole.USER.value or not isinstance(item.get("content"), str):
                    continue
                try:
                    envelope = json.loads(item["content"])
                except json.JSONDecodeError:
                    continue
                if not isinstance(envelope, dict) or not isinstance(envelope.get("user_message"), str):
                    continue
                try:
                    background_result = json.loads(envelope["user_message"])
                except json.JSONDecodeError:
                    continue
                if isinstance(background_result, dict) and background_result.get("type") == "background_tool_result":
                    background_results.append(background_result)
            assert len(background_results) == 1
            return background_results[0]

        def assert_background_result_payload(payload: dict) -> None:
            assert payload["type"] == "background_tool_result"
            assert payload["task"] == task_result
            assert payload["task_id"] == task_id
            assert payload["tool_name"] == "generate_image"
            assert payload["tool_call"]["id"] == "generate-image-call"
            assert payload["tool_result"]["tool_call_id"] == "generate-image-call"
            assert json.loads(payload["tool_result"]["content"]) == task_result
            result_path = payload["task"]["content"]["send_file_to_user"]["files"][0]["path"]
            assert result_path == str(image_path)

        def assert_persisted_background_result(messages: list[Message]) -> Message:
            background_results = [message for message in messages if message.role == MessageRole.USER and message.type == MessageType.BACKGROUND_TASK_RESULT]
            assert len(background_results) == 1
            assert not any(message.role == MessageRole.USER and message.type != MessageType.BACKGROUND_TASK_RESULT for message in messages)
            background_result = background_results[0]
            assert background_result.dedupe_key == f"background-task:{task_id}:result"
            assert background_result.is_processed is True
            payload = json.loads(background_result.content or "{}")
            assert_background_result_payload(payload)
            return background_result

        def find_send_file_call(items: list[dict]) -> dict:
            if protocol == "OPENAI":
                return next(tool_call for item in items if item.get("role") == MessageRole.ASSISTANT.value for tool_call in item.get("tool_calls") or [] if tool_call.get("function", {}).get("name") == "send_file_to_user")
            return next(item for item in items if item.get("type") == "function_call" and item.get("name") == "send_file_to_user")

        def tool_names(payload: dict) -> list[str]:
            tools = payload.get("tools") or []
            if protocol == "OPENAI":
                return [tool["function"]["name"] for tool in tools]
            return [tool["name"] for tool in tools]

        async def fake_post_json(_transformer, **kwargs):
            payload = kwargs["payload"]
            provider_payloads.append(payload)
            items = provider_items(payload)
            system_indexes = [index for index, item in enumerate(items) if item.get("role") == MessageRole.SYSTEM.value]
            assert system_indexes == [0]
            _guidance_index, guidance = find_guidance(items)
            request_index = len(provider_payloads) - 1
            if request_index == 0:
                assert guidance == {
                    "user_message": None,
                    "platform_guidance": BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT,
                }
            else:
                expected_guidance = f"{BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT}\n\n{BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT}"
                assert guidance == {
                    "user_message": None,
                    "platform_guidance": expected_guidance,
                }

            if request_index == 0:
                assert tool_names(payload) == ["send_file_to_user"]
                background_result = find_background_result(items)
                assert_background_result_payload(background_result)
                assert not any((item.get("role") == MessageRole.TOOL.value and item.get("tool_call_id") == "generate-image-call") or (item.get("type") == "function_call_output" and item.get("call_id") == "generate-image-call") for item in items)
                result_path = background_result["task"]["content"]["send_file_to_user"]["files"][0]["path"]
                arguments = json.dumps({"files": [{"path": result_path}]}, ensure_ascii=False)
                if protocol == "OPENAI":
                    return {
                        "id": "chatcmpl-background-image",
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
                                            "id": "send-file-call",
                                            "type": "function",
                                            "function": {
                                                "name": "send_file_to_user",
                                                "arguments": arguments,
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                return {
                    "id": "resp-background-image",
                    "object": "response",
                    "model": MODEL_ID,
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "send-file-call",
                            "call_id": "send-file-call",
                            "name": "send_file_to_user",
                            "arguments": arguments,
                            "status": "completed",
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }

            assert payload.get("tools") == provider_payloads[0].get("tools")
            assert tool_names(payload) == ["send_file_to_user"]
            assert payload["tool_choice"] == "auto"
            send_file_call = find_send_file_call(items)
            if protocol == "OPENAI":
                send_file_call_id = send_file_call["id"]
                send_file_arguments = json.loads(send_file_call["function"]["arguments"])
                actual_function_calls = [call for item in items if item.get("role") == MessageRole.ASSISTANT.value for call in item.get("tool_calls") or []]
                actual_call_ids = [call["id"] for call in actual_function_calls]
            else:
                send_file_call_id = send_file_call["call_id"]
                send_file_arguments = json.loads(send_file_call["arguments"])
                actual_function_calls = [item for item in items if item.get("type") == "function_call"]
                actual_call_ids = [call["call_id"] for call in actual_function_calls]
            assert send_file_arguments == {"files": [{"path": str(image_path)}]}

            rejected_calls = [call for call, call_id in zip(actual_function_calls, actual_call_ids) if call_id != send_file_call_id]
            rejected_call_ids = [call_id for call_id in actual_call_ids if call_id != send_file_call_id]
            violation_rounds = len(invalid_tool_call_ids) // 2
            assert len(rejected_calls) == len(invalid_tool_call_ids)
            rejected_names = [call["function"]["name"] if protocol == "OPENAI" else call["name"] for call in rejected_calls]
            assert rejected_names.count("send_file_to_user") == violation_rounds
            assert rejected_names.count("generate_image") == violation_rounds
            for rejected_call, call_id in zip(rejected_calls, rejected_call_ids):
                rejected_result = find_tool_output(items, call_id)
                assert rejected_result["status"] == "rejected"
                assert rejected_result["executed"] is False
                assert rejected_result["instruction"] == BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT
                assert rejected_result["tool_call"]["id"] == call_id

            send_file_result = find_tool_output(items, send_file_call_id)
            assert send_file_result["type"] == "files_to_user"
            assert send_file_result["status"] == "success"
            assert len(send_file_result["files"]) == 1
            final_request_index = request_index - 1
            if final_request_index < final_tool_violations:
                round_number = final_request_index + 1
                send_call_id = f"final-violation-{round_number}-send-file-call"
                generate_call_id = f"final-violation-{round_number}-generate-image-call"
                invalid_tool_call_ids.extend([send_call_id, generate_call_id])
                send_arguments = json.dumps({"files": [{"path": str(image_path)}]}, ensure_ascii=False)
                generate_arguments = json.dumps(
                    {"prompt": "do not generate", "run_in_background": True},
                    ensure_ascii=False,
                )
                if protocol == "OPENAI":
                    return {
                        "id": f"chatcmpl-background-image-violation-{round_number}",
                        "object": "chat.completion",
                        "model": MODEL_ID,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "伪成功正文",
                                    "tool_calls": [
                                        {
                                            "id": send_call_id,
                                            "type": "function",
                                            "function": {
                                                "name": "send_file_to_user",
                                                "arguments": send_arguments,
                                            },
                                        },
                                        {
                                            "id": generate_call_id,
                                            "type": "function",
                                            "function": {
                                                "name": "generate_image",
                                                "arguments": generate_arguments,
                                            },
                                        },
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                return {
                    "id": f"resp-background-image-violation-{round_number}",
                    "object": "response",
                    "model": MODEL_ID,
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "伪成功正文"}],
                        },
                        {
                            "type": "function_call",
                            "id": send_call_id,
                            "call_id": send_call_id,
                            "name": "send_file_to_user",
                            "arguments": send_arguments,
                            "status": "completed",
                        },
                        {
                            "type": "function_call",
                            "id": generate_call_id,
                            "call_id": generate_call_id,
                            "name": "generate_image",
                            "arguments": generate_arguments,
                            "status": "completed",
                        },
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }

            if protocol == "OPENAI":
                return {
                    "id": "chatcmpl-background-image-final",
                    "object": "chat.completion",
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "后台图片已发送"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            return {
                "id": "resp-background-image-final",
                "object": "response",
                "model": MODEL_ID,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "后台图片已发送"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

        events = []

        async def capture_send_session_event(uid: str, session_id: str, event: dict) -> None:
            events.append((uid, session_id, event))

        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
        transformer_cls = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
        monkeypatch.setattr(transformer_cls, "_post_json", fake_post_json)
        monkeypatch.setattr(consumer_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(executor_lifecycle_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(executor_metadata_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(send_file_to_user_module, "_get_encryption_key", lambda: b"t" * 32)
        monkeypatch.setattr(send_file_to_user_module, "FILE_TOKEN_DIR", tmp_path / "file_download_tokens")
        monkeypatch.setattr(executor_lifecycle_module, "send_session_event", capture_send_session_event)

        await consumer_module.SessionReplyConsumer()._run_claimed(
            work_id,
            "image-worker",
            attempt_count=1,
            max_attempts=3,
        )

        expected_request_count = 2 + min(final_tool_violations, 3)
        assert len(provider_payloads) == expected_request_count
        first_items = provider_items(provider_payloads[0])
        first_system = next(item for item in first_items if item.get("role") == MessageRole.SYSTEM.value)
        for payload in provider_payloads[1:]:
            final_items = provider_items(payload)
            final_system = next(item for item in final_items if item.get("role") == MessageRole.SYSTEM.value)
            assert first_system["content"] == final_system["content"]
        first_prefix = first_items[:-1]
        for payload in provider_payloads[1:]:
            final_prefix = provider_items(payload)[:-1]
            assert final_prefix[: len(first_prefix)] == first_prefix

        if final_tool_violations == 4:
            expected_error = t(ERR_LLM_FINAL_REPLY_TOOL_CORRECTION_FAILED)
            assert len(provider_payloads) == 5
            assert len(events) == 1
            event_uid, event_session_id, event = events[0]
            assert (event_uid, event_session_id) == (UID, SESSION_ID)
            assert event["type"] == "proactive_reply_error"
            assert event["content"] == expected_error
            assert "伪成功正文" not in event["content"]

            async with factory() as db:
                persisted_work = await db.get(SessionReplyWorkItem, work_id)
                persisted_task = await db.get(BackgroundTask, task_id)
                assert persisted_work is not None
                assert persisted_task is not None
                assert persisted_work.status == SessionReplyWorkStatus.FAILED
                assert persisted_work.attempt_count == 1
                assert persisted_task.status == BackgroundTaskStatus.SUCCEEDED
                assert persisted_task.reply_status == BackgroundTaskReplyStatus.FAILED
                assert persisted_task.result == task_result
                persisted_work_items = (await db.execute(select(SessionReplyWorkItem))).scalars().all()
                assert len(persisted_work_items) == 1
                persisted_tasks = (await db.execute(select(BackgroundTask))).scalars().all()
                assert len(persisted_tasks) == 1
                persisted_messages = (await db.execute(select(Message).where(Message.session_id == SESSION_ID))).scalars().all()
                assert_persisted_background_result(persisted_messages)
                tool_calls = [message for message in persisted_messages if message.type == MessageType.TOOL_CALL]
                tool_results = [message for message in persisted_messages if message.type == MessageType.TOOL_RESULT]
                assert len(tool_calls) == 1
                assert len(tool_results) == 1
                persisted_dump = json.dumps([message.content for message in persisted_messages], ensure_ascii=False)
                persisted_tool_dump = json.dumps([message.content for message in [*tool_calls, *tool_results]], ensure_ascii=False)
                persisted_send_call = InternalMessage.model_validate_json(tool_calls[0].content)
                assert len(persisted_send_call.tool_calls or []) == 1
                persisted_send_tool_call = persisted_send_call.tool_calls[0]
                assert persisted_send_tool_call.name == "send_file_to_user"
                persisted_send_result = InternalMessage.model_validate_json(tool_results[0].content)
                assert persisted_send_result.tool_call_id == persisted_send_tool_call.id
                assert json.loads(persisted_send_result.content or "{}")["status"] == "success"
                assert "generate_image" not in persisted_tool_dump
                assert "final-violation-" not in persisted_dump
                assert "伪成功正文" not in persisted_dump
                assert BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT not in persisted_dump
                assert BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT not in persisted_dump
                assert BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT not in persisted_dump
                error_texts = [message for message in persisted_messages if message.role == MessageRole.ERR and message.type == MessageType.TEXT]
                assert len(error_texts) == 1
                assert error_texts[0].content == expected_error
                assistant_texts = [message for message in persisted_messages if message.role == MessageRole.ASSISTANT and message.type == MessageType.TEXT]
                assert len(assistant_texts) == 0
                assert "assistant_files" not in persisted_dump

            await executor_lifecycle_module.execute_session_reply_work(work_id, "image-worker")
            assert len(provider_payloads) == 5
            assert len(events) == 1
            return

        assert len(events) == 1
        event_uid, event_session_id, event = events[0]
        assert (event_uid, event_session_id) == (UID, SESSION_ID)
        assert event["type"] == "proactive_reply"
        assert event["content"] == "后台图片已发送"
        assert len(event["files"]) == 1
        assert event["files"][0]["mime_type"] == "image/png"
        assert event["files"][0]["size"] == image_size
        assert send_file_to_user_module.resolve_file_token(event["files"][0]["id"]) == image_path.resolve()
        event_dump = json.dumps(event, ensure_ascii=False)
        assert BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT not in event_dump
        assert BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT not in event_dump
        assert BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT not in event_dump
        assert not any(item.get("role") == MessageRole.USER.value for item in event.get("history", []))

        async with factory() as db:
            persisted_work = await db.get(SessionReplyWorkItem, work_id)
            persisted_task = await db.get(BackgroundTask, task_id)
            assert persisted_work is not None
            assert persisted_task is not None
            assert persisted_work.status == SessionReplyWorkStatus.SUCCEEDED
            assert persisted_work.result_message_id is not None
            assert persisted_task.reply_status == BackgroundTaskReplyStatus.SUCCEEDED
            persisted_messages = (await db.execute(select(Message).where(Message.session_id == SESSION_ID))).scalars().all()
            persisted_background_result = assert_persisted_background_result(persisted_messages)
            persisted_message_dump = json.dumps([message.content for message in persisted_messages], ensure_ascii=False)
            assert "final-violation-" not in persisted_message_dump
            assert "伪成功正文" not in persisted_message_dump
            assert all(message.guidance_prompt is None for message in persisted_messages)
            persisted_response = (persisted_work.execution_state or {}).get("response")
            assert isinstance(persisted_response, dict)
            assert persisted_response["content"] == "后台图片已发送"
            persisted_response_dump = json.dumps(persisted_response, ensure_ascii=False)
            assert BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT not in persisted_response_dump
            assert BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT not in persisted_response_dump
            assert BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT not in persisted_response_dump
            assert not any(item.get("role") == MessageRole.USER.value for item in persisted_response.get("history", []))
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
                    reply_work = await client.get(f"/api/v1/chat/reply-works/{work_id}")

            assert pending_activity.status_code == 200
            assert pending_activity.json()["data"] == {"has_pending_activity": False}
            assert history.status_code == 200
            assert reply_work.status_code == 200
            reply_work_data = reply_work.json()["data"]
            assert reply_work_data["response"]["content"] == "后台图片已发送"
            reply_work_dump = json.dumps(reply_work_data, ensure_ascii=False)
            assert BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT not in reply_work_dump
            assert BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT not in reply_work_dump
            assert BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT not in reply_work_dump
            assert not any(item.get("role") == MessageRole.USER.value for item in reply_work_data["response"].get("history", []))
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
        history_dump = json.dumps(history_items, ensure_ascii=False)
        assert BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT not in history_dump
        assert BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT not in history_dump
        assert BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT not in history_dump
        background_history_items = [item for item in history_items if item.get("role") == MessageRole.USER.value and item.get("type") == MessageType.BACKGROUND_TASK_RESULT.value]
        assert len(background_history_items) == 1
        background_history_item = background_history_items[0]
        assert background_history_item["id"] == persisted_background_result.id
        assert_background_result_payload(json.loads(background_history_item["content"]))
        assert not any(item.get("role") == MessageRole.USER.value and item.get("type") != MessageType.BACKGROUND_TASK_RESULT.value for item in history_items)
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
        assert len(provider_payloads) == expected_request_count
        assert len(events) == 1


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_count: int,
    max_parallel_tools: int,
    target_index: int,
    sibling_indexes: tuple[int, ...],
    sibling_result_kind: str,
    history_id_reuse: str,
) -> None:
    def image_payload(file_name: str) -> dict:
        path = tmp_path / file_name
        image = Image.new("RGB", (1, 1))
        try:
            image.save(path, format="PNG")
        finally:
            image.close()
        return json.loads(
            ImageGenerationExecutor(str(tmp_path), UID)._build_success_payload(
                {
                    "path": str(path),
                    "name": file_name,
                    "description": "Generated image",
                    "mime_type": "image/png",
                }
            )
        )

    history_path = tmp_path / "history.txt"
    report_path = tmp_path / "report.txt"
    history_path.write_text("Historical file contents\n", encoding="utf-8")
    report_path.write_text("Current report contents\n", encoding="utf-8")

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
                    content=json.dumps(read_file(history_path, start_line=None, end_line=None), ensure_ascii=False),
                ),
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=history_calls[0].id,
                    content=json.dumps(image_payload("history.png"), ensure_ascii=False),
                ),
            ]
            history_assistant = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=history_calls)
            rejected_results = handle_parallel_tool_limit(current_calls, max_parallel_tools)[1]
            sibling_results = []
            for index in sibling_indexes:
                sibling_call = current_calls[index]
                if sibling_result_kind == "file_success":
                    sibling_content = read_file(report_path, start_line=None, end_line=None)
                else:
                    sibling_content = json.loads(_build_background_task_queued_result(sibling_call.name, 100 + index))
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
            assert current_user.id is not None
            expected_request_context = [
                {
                    "id": current_user.id,
                    "role": MessageRole.USER.value,
                    "content": "当前用户文本",
                }
            ]
            assert isinstance(task.extra, dict)
            assert task.extra["request_context"] == expected_request_context
            assert "submission_context" not in task.extra

            existing_result_call_ids = {message.tool_call_id for message in current_results}
            for index, current_call in enumerate(current_calls):
                if current_call.id in existing_result_call_ids:
                    continue
                queued_task_id = task.id if index == target_index else 100 + index
                queued_result = InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=current_call.id,
                    content=_build_background_task_queued_result(current_call.name, queued_task_id),
                )
                saved_queued_result = await save_message(
                    db,
                    SESSION_ID,
                    UID,
                    MessageRole.TOOL,
                    MessageType.TOOL_RESULT,
                    queued_result,
                    profile.id,
                )
                assert saved_queued_result.id is not None
                queued_result.id = saved_queued_result.id
                current_results.append(queued_result)
                existing_result_call_ids.add(current_call.id)
                original_message_contents[saved_queued_result.id] = saved_queued_result.content

        task_result = build_background_task_success_result(
            "generate_image",
            image_payload(f"current-{target_index + 1}.png"),
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
            db.add(
                Message(
                    session_id=SESSION_ID,
                    uid=UID,
                    profile_id=profile.id,
                    role=MessageRole.USER,
                    type=MessageType.BACKGROUND_TASK_RESULT,
                    content=reply_trigger_module._build_background_tool_result_message(persisted_task),
                    is_processed=True,
                    dedupe_key=f"background-task:{task_id}:result",
                )
            )
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

        expected_function_calls = [(call.id, call.arguments) for call in history_calls] + [(call.id, call.arguments) for call in current_calls]
        expected_function_call_outputs = [(message.tool_call_id, json.loads(message.content or "{}")) for message in history_results] + [(message.tool_call_id, json.loads(message.content or "{}")) for message in current_results]
        target_protocol_result = next(message for message in reversed(current_results) if message.tool_call_id == target_call.id)
        target_protocol_payload = json.loads(target_protocol_result.content or "{}")
        assert target_protocol_payload["status"] == "queued"
        assert target_protocol_payload["task_id"] == task_id
        assert target_protocol_payload != task_result
        provider_payloads = []

        async def fake_post_json(_transformer, **kwargs):
            payload = kwargs["payload"]
            provider_payloads.append(payload)
            assert "tools" not in payload
            assert payload["tool_choice"] == "none"
            input_items = payload["input"]
            assert input_items
            assert input_items[-1].get("role") == "user"
            final_user_content = input_items[-1].get("content")
            assert isinstance(final_user_content, str) and final_user_content.strip()
            final_user_payload = json.loads(final_user_content)
            assert final_user_payload["user_message"] is None
            platform_guidance = final_user_payload["platform_guidance"]
            expected_platform_guidance = f"{BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT}\n\n{BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT}"
            assert platform_guidance == expected_platform_guidance
            assert platform_guidance.count(BACKGROUND_PROACTIVE_TOOL_USAGE_GUIDANCE_PROMPT) == 1
            assert platform_guidance.count(BACKGROUND_PROACTIVE_FINAL_REPLY_GUIDANCE_PROMPT) == 1
            for item in input_items[:-1]:
                if item.get("role") != "user" or not isinstance(item.get("content"), str):
                    continue
                assert "platform_guidance" not in json.loads(item["content"])
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
            background_results = []
            for item in input_items:
                if item.get("role") != "user" or not isinstance(item.get("content"), str):
                    continue
                try:
                    user_payload = json.loads(item["content"])
                except json.JSONDecodeError:
                    continue
                if isinstance(user_payload, dict):
                    user_message = user_payload.get("user_message")
                    user_messages.append(user_message)
                    if isinstance(user_message, str):
                        try:
                            background_result = json.loads(user_message)
                        except json.JSONDecodeError:
                            background_result = None
                        if isinstance(background_result, dict) and background_result.get("type") == "background_tool_result":
                            background_results.append(background_result)
            assert "此前用户文本" in user_messages
            assert "当前用户文本" in user_messages
            assert any(item.get("role") == "assistant" and item.get("content") == "此前助手文本" for item in input_items)
            assert len(background_results) == 1
            background_result = background_results[0]
            assert background_result["task_id"] == task_id
            assert background_result["task"] == task_result
            assert background_result["tool_call"] == {
                "id": target_call.id,
                "name": "generate_image",
                "arguments": target_call.arguments,
            }
            assert background_result["tool_result"]["tool_call_id"] == target_call.id
            assert json.loads(background_result["tool_result"]["content"]) == task_result
            assert background_result["request_context"] == expected_request_context
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
            assert persisted_task.extra["request_context"] == expected_request_context
            assert "submission_context" not in persisted_task.extra
            persisted_background_results = [message for message in (await db.execute(select(Message).where(Message.session_id == SESSION_ID))).scalars().all() if message.role == MessageRole.USER and message.type == MessageType.BACKGROUND_TASK_RESULT]
            assert len(persisted_background_results) == 1
            persisted_background_result = persisted_background_results[0]
            assert persisted_background_result.dedupe_key == f"background-task:{task_id}:result"
            assert persisted_background_result.is_processed is True
            persisted_background_payload = json.loads(persisted_background_result.content or "{}")
            assert persisted_background_payload["task_id"] == task_id
            assert persisted_background_payload["task"] == task_result
            assert persisted_background_payload["tool_call"] == {
                "id": target_call.id,
                "name": "generate_image",
                "arguments": target_call.arguments,
            }
            assert persisted_background_payload["request_context"] == expected_request_context

            result_message = await db.get(Message, persisted_work.result_message_id)
            assert result_message is not None
            assert result_message.role == MessageRole.ASSISTANT
            assert result_message.type == MessageType.TEXT
            assert result_message.content == "当前图片生成完成"
            for message_id, original_content in original_message_contents.items():
                original_message = await db.get(Message, message_id)
                assert original_message is not None
                assert original_message.content == original_content
