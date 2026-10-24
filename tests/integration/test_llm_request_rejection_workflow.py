import json
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from sqlmodel import select

import app.core.background_tasks.reply_trigger as reply_trigger_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
import app.core.message_platforms.notifier as message_platform_notifier_module
import app.core.session.notifier as session_notifier_module
import app.core.session_reply_queue.consumer as consumer_module
import app.core.session_reply_queue.executor_interactive as executor_interactive_module
import app.core.session_reply_queue.executor_lifecycle as executor_lifecycle_module
import app.core.session_reply_queue.executor_metadata as executor_metadata_module
import app.core.utils.dispatcher.helpers as dispatcher_helpers_module
import app.core.utils.llm_request_policy as llm_request_policy_module
import app.providers.database as database_provider
import app.transformers.openai.base as openai_base_module
import app.transformers.openai.image_generation as image_generation_module
from app.api.v1.chat import router
from app.core.exceptions import LLMRequestRejectedException
from app.core.security import get_current_user
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.handler import register_handlers
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.message_platform_outbox import MessagePlatformOutbox
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.providers.database import get_db
from app.providers.image_generation.client import ImageGenerationClient
from app.providers.llm.client import LLMClient

UID = "llm-rejection-user"
SESSION_ID = "llm-rejection-session"
MODEL_ID = "llm-rejection-model"
WORKER_ID = "llm-rejection-worker"
LARGE_TEXT = "测" * 380_000
UPSTREAM_HTML = "<html><body>provider-private-413-body</body></html>"
SUCCESS_CONTENT = "真实标准响应"


@dataclass
class _SeededWork:
    work_id: int
    profile_id: int
    task_id: int | None
    task_result: dict[str, Any] | None


class _FakeStreamContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    def iter_any(self) -> "_FakeStreamContent":
        return self

    def __aiter__(self) -> "_FakeStreamContent":
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeAiohttpResponse:
    def __init__(self, *, status: int, text: str, chunks: list[bytes] | None = None) -> None:
        self.status = status
        self._text = text
        self.content = _FakeStreamContent(chunks or [])

    async def __aenter__(self) -> "_FakeAiohttpResponse":
        return self

    async def __aexit__(self, _exc_type, _exc_value, _traceback) -> bool:
        return False

    async def text(self) -> str:
        return self._text


class _FakeHttp:
    def __init__(self, protocol: str, outcomes: list[str]) -> None:
        self.protocol = protocol
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []
        self.session_calls: list[dict[str, Any]] = []

    def _response(self, outcome: str, *, stream: bool) -> _FakeAiohttpResponse:
        if outcome == "413":
            return _FakeAiohttpResponse(status=413, text=UPSTREAM_HTML)

        if self.protocol == "openai":
            body: dict[str, Any] = {
                "id": "chatcmpl-rejection-success",
                "object": "chat.completion",
                "model": MODEL_ID,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": SUCCESS_CONTENT},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }
            if not stream:
                return _FakeAiohttpResponse(status=200, text=json.dumps(body, ensure_ascii=False))
            events = [
                {
                    "id": body["id"],
                    "model": MODEL_ID,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": SUCCESS_CONTENT}, "finish_reason": None}],
                },
                {
                    "id": body["id"],
                    "model": MODEL_ID,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": body["usage"],
                },
            ]
        else:
            body = {
                "id": "resp-rejection-success",
                "object": "response",
                "status": "completed",
                "model": MODEL_ID,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": SUCCESS_CONTENT}],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            }
            if not stream:
                return _FakeAiohttpResponse(status=200, text=json.dumps(body, ensure_ascii=False))
            events = [
                {"type": "response.output_text.delta", "delta": SUCCESS_CONTENT, "output_index": 0, "content_index": 0},
                {"type": "response.completed", "response": body},
            ]

        chunks = [f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode() for event in events]
        chunks.append(b"data: [DONE]\n\n")
        return _FakeAiohttpResponse(status=200, text=json.dumps(body, ensure_ascii=False), chunks=chunks)

    def client_session(self, *args: Any, **kwargs: Any) -> "_FakeClientSession":
        self.session_calls.append({"args": args, "kwargs": kwargs})
        return _FakeClientSession(self)

    def next_response(self, *, stream: bool) -> _FakeAiohttpResponse:
        call_index = max(0, len(self.calls) - 1)
        outcome = self.outcomes[min(call_index, len(self.outcomes) - 1)]
        return self._response(outcome, stream=stream)


class _FakeClientSession:
    def __init__(self, http: _FakeHttp) -> None:
        self._http = http

    async def __aenter__(self) -> "_FakeClientSession":
        return self

    async def __aexit__(self, _exc_type, _exc_value, _traceback) -> bool:
        return False

    def post(self, url: str, **kwargs: Any) -> _FakeAiohttpResponse:
        stream = bool(json.loads(kwargs["data"]).get("stream", False))
        self._http.calls.append({"url": url, "kwargs": kwargs})
        return self._http.next_response(stream=stream)


class _CountingWriter:
    def __init__(self) -> None:
        self.size = 0

    async def write(self, data: bytes) -> None:
        self.size += len(data)


class _FakeImageClientSession:
    def __init__(self, http: "_FakeImageHttp") -> None:
        self._http = http

    async def __aenter__(self) -> "_FakeImageClientSession":
        return self

    async def __aexit__(self, _exc_type, _exc_value, _traceback) -> bool:
        return False

    def post(self, url: str, **kwargs: Any) -> _FakeAiohttpResponse:
        self._http.post_calls.append({"url": url, "kwargs": kwargs})
        return _FakeAiohttpResponse(status=200, text='{"data": []}')


class _FakeImageHttp:
    def __init__(self) -> None:
        self.session_calls: list[dict[str, Any]] = []
        self.connector_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    def tcp_connector(self, *args: Any, **kwargs: Any) -> object:
        self.connector_calls.append({"args": args, "kwargs": kwargs})
        return object()

    def client_session(self, *args: Any, **kwargs: Any) -> _FakeImageClientSession:
        self.session_calls.append({"args": args, "kwargs": kwargs})
        return _FakeImageClientSession(self)


def _install_fake_http(monkeypatch: pytest.MonkeyPatch, http: _FakeHttp) -> None:
    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", http.client_session)
    monkeypatch.setattr(openai_base_module.aiohttp, "TCPConnector", lambda **_kwargs: object())


def _install_fake_image_http(monkeypatch: pytest.MonkeyPatch, http: _FakeImageHttp) -> None:
    monkeypatch.setattr(image_generation_module.aiohttp, "ClientSession", http.client_session)
    monkeypatch.setattr(image_generation_module.aiohttp, "TCPConnector", http.tcp_connector)


def _profile_config() -> dict[str, Any]:
    config = ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "rules": [
                        {"channel_id": 1, "model_id": MODEL_ID, "priority": 1, "weight": 1},
                        {"channel_id": 2, "model_id": MODEL_ID, "priority": 2, "weight": 1},
                    ]
                }
            },
            "security": {"audit_threshold": 0},
            "tool": {"enabled_tools": []},
            "other": {},
            "memory": {"enabled": False},
        }
    )
    return config.model_dump(mode="json")


async def _seed_work(
    factory,
    *,
    protocol: str,
    work_type: SessionReplyWorkType,
    source: str = "http",
    stream_requested: bool = False,
    content: str = LARGE_TEXT,
) -> _SeededWork:
    normalized_protocol = protocol.upper()
    configs = _profile_config()

    async with factory() as db:
        db.add_all(
            [
                User(uid=UID, username="llm_rejection_user"),
                ModelChannel(
                    id=1,
                    name="llm-rejection-channel-1",
                    api_key="enc:v1:stored-key-1",
                    base_url="https://channel-1.invalid",
                    model_ids=[
                        {
                            "model_id": MODEL_ID,
                            "usage": "CHAT",
                            "protocol": normalized_protocol,
                            "context_window_k": 4096,
                            "max_tokens": 512,
                        }
                    ],
                ),
                ModelChannel(
                    id=2,
                    name="llm-rejection-channel-2",
                    api_key="enc:v1:stored-key-2",
                    base_url="https://channel-2.invalid",
                    model_ids=[
                        {
                            "model_id": MODEL_ID,
                            "usage": "CHAT",
                            "protocol": normalized_protocol,
                            "context_window_k": 4096,
                            "max_tokens": 512,
                        }
                    ],
                ),
                SystemSetting(key="llm_request_max_size_mb", value="1"),
            ]
        )
        profile = Profile(uid=UID, name="llm rejection profile", configs=configs)
        db.add(profile)
        await db.flush()
        assert profile.id is not None
        db.add(
            ChatSession(
                session_id=SESSION_ID,
                uid=UID,
                profile_id=profile.id,
                source=source,
                reply_target_source=source,
                show_tool_calls=True,
            )
        )
        await db.flush()

        task: BackgroundTask | None = None
        source_id: str
        source_type: SessionReplySourceType
        if work_type == SessionReplyWorkType.FOREGROUND_REPLY:
            user_message = Message(
                session_id=SESSION_ID,
                uid=UID,
                profile_id=profile.id,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content=content,
                is_processed=False,
            )
            db.add(user_message)
            await db.flush()
            assert user_message.id is not None
            source_id = str(user_message.id)
            source_type = SessionReplySourceType.USER_MESSAGE
        else:
            task_result = {
                "status": "succeeded",
                "tool_name": "generate_image",
                "content": {"marker": "keep-this-result"},
            }
            task = BackgroundTask(
                uid=UID,
                session_id=SESSION_ID,
                profile_id=profile.id,
                tool_call_id="llm-rejection-background-call",
                tool_name="generate_image",
                status=BackgroundTaskStatus.SUCCEEDED,
                arguments={"prompt": "keep"},
                result=task_result,
                auto_reply=True,
                reply_status=BackgroundTaskReplyStatus.RUNNING,
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
                    type=MessageType.TEXT,
                    content=content,
                    is_processed=True,
                )
            )
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
            source_id = str(task.id)
            source_type = SessionReplySourceType.BACKGROUND_TASK

        work = SessionReplyWorkItem(
            uid=UID,
            session_id=SESSION_ID,
            profile_id=profile.id,
            sequence_no=1,
            work_type=work_type,
            source_type=source_type,
            source_id=source_id,
            dedupe_key=f"llm-rejection:{work_type.value}:{source}",
            status=SessionReplyWorkStatus.RUNNING,
            execution_state={
                "stream_requested": stream_requested,
                "message_source": source,
                "request_ids": [f"request-{work_type.value}-{source}"],
                "show_tool_calls": True,
                "expose_tool_call_content": True,
                "actor_uid": UID,
                "actor_username": "llm_rejection_user",
                "language": "zh",
            },
            locked_by=WORKER_ID,
            lock_until=int(time.time()) + 300,
            attempt_count=1,
            max_attempts=2,
        )
        db.add(work)
        await db.commit()
        await db.refresh(work)
        assert work.id is not None
        return _SeededWork(
            work_id=work.id,
            profile_id=profile.id,
            task_id=task.id if task is not None else None,
            task_result=task.result if task is not None else None,
        )


def _patch_queue_databases(monkeypatch: pytest.MonkeyPatch, factory) -> None:
    for module in (
        llm_request_policy_module,
        consumer_module,
        executor_lifecycle_module,
        executor_metadata_module,
        executor_interactive_module,
        dispatcher_module,
        dispatcher_helpers_module,
        channel_cursor_module,
        message_platform_notifier_module,
        session_notifier_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(database_provider, "AsyncSessionLocal", factory)


def _build_http_app(factory) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(uid=UID, is_superuser=False)
    return app


async def _set_request_limit(factory, value: int) -> None:
    async with factory() as db:
        await db.execute(update(SystemSetting).where(SystemSetting.key == "llm_request_max_size_mb").values(value=str(value)))
        await db.commit()


async def _persisted_failure(factory, work_id: int) -> tuple[SessionReplyWorkItem, Message, list[Message]]:
    async with factory() as db:
        work = await db.get(SessionReplyWorkItem, work_id)
        assert work is not None
        assert work.result_message_id is not None
        error_message = await db.get(Message, work.result_message_id)
        assert error_message is not None
        messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
        return work, error_message, messages


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
@pytest.mark.parametrize(
    ("work_type", "stream_requested"),
    [
        (SessionReplyWorkType.FOREGROUND_REPLY, False),
        (SessionReplyWorkType.FOREGROUND_REPLY, True),
        (SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, False),
    ],
)
@pytest.mark.parametrize("error_mode", ["local", "upstream413"])
async def test_llm_request_rejection_fails_claimed_work_and_notifies_user(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    stream_requested: bool,
    work_type: SessionReplyWorkType,
    error_mode: str,
) -> None:
    async with database_factory(foreign_keys=True, wal=True) as factory:
        seeded = await _seed_work(
            factory,
            protocol=protocol,
            work_type=work_type,
            stream_requested=stream_requested,
        )
        _patch_queue_databases(monkeypatch, factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
        http = _FakeHttp(protocol, ["413", "413"] if error_mode == "upstream413" else ["200"])
        _install_fake_http(monkeypatch, http)
        if error_mode == "upstream413":
            await _set_request_limit(factory, 2)

        consumer = consumer_module.SessionReplyConsumer()
        await consumer._run_claimed(seeded.work_id, WORKER_ID, 1, 2, "zh")

        failed, error_message, messages = await _persisted_failure(factory, seeded.work_id)
        assert failed.status == SessionReplyWorkStatus.FAILED
        assert failed.attempt_count == 1
        assert failed.locked_by is None
        assert error_message.role == MessageRole.ERR
        assert error_message.type == MessageType.TEXT
        assert error_message.content
        assert UPSTREAM_HTML not in error_message.content
        assert UPSTREAM_HTML not in (failed.error or "")
        assert "全局" in error_message.content
        assert len([message for message in messages if message.role == MessageRole.ERR]) == 1
        if error_mode == "local":
            assert "本地拦截" in error_message.content
            assert http.calls == []
        else:
            assert "413" in error_message.content
            assert len(http.calls) == 2
            assert [call["url"] for call in http.calls] == [
                "https://channel-1.invalid/chat/completions" if protocol == "openai" else "https://channel-1.invalid/responses",
                "https://channel-2.invalid/chat/completions" if protocol == "openai" else "https://channel-2.invalid/responses",
            ]

        async with factory() as db:
            status = await session_reply_queue_manager.get_work_status(db, seeded.work_id, uid=UID)
            assert status["status"] == SessionReplyWorkStatus.FAILED.value
            assert status["error"] == error_message.content
            events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == SESSION_ID))).scalars().all())
            assert len(events) == 1
            assert events[0].event["type"] == "proactive_reply_error"
            assert events[0].event["content"] == error_message.content

            if seeded.task_id is not None:
                task = await db.get(BackgroundTask, seeded.task_id)
                assert task is not None
                assert task.status == BackgroundTaskStatus.SUCCEEDED
                assert task.result == seeded.task_result
                assert task.reply_status == BackgroundTaskReplyStatus.FAILED

        if stream_requested:
            stream_events = [event async for event in session_reply_queue_manager.wait_for_stream(seeded.work_id)]
            error_events = [event for event in stream_events if event.get("type") == "error"]
            assert len(error_events) == 1
            assert error_events[0]["message"] == error_message.content
            assert error_events[0]["work_id"] == seeded.work_id


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "weixin-openclaw"])
async def test_rejection_notification_uses_real_web_event_or_external_outbox(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    async with database_factory(foreign_keys=True, wal=True) as factory:
        seeded = await _seed_work(
            factory,
            protocol="openai",
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source=source,
            content=LARGE_TEXT,
        )
        _patch_queue_databases(monkeypatch, factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
        http = _FakeHttp("openai", ["200"])
        _install_fake_http(monkeypatch, http)

        await consumer_module.SessionReplyConsumer()._run_claimed(seeded.work_id, WORKER_ID, 1, 2, "zh")
        failed, error_message, _messages = await _persisted_failure(factory, seeded.work_id)
        assert failed.status == SessionReplyWorkStatus.FAILED

        async with factory() as db:
            if source == "http":
                session_events = list((await db.execute(select(SessionEvent).where(SessionEvent.session_id == SESSION_ID))).scalars().all())
                assert len(session_events) == 1
                assert session_events[0].event["type"] == "proactive_reply_error"
                assert session_events[0].event["content"] == error_message.content
                assert list((await db.execute(select(MessagePlatformOutbox))).scalars().all()) == []
            else:
                outbox_items = list((await db.execute(select(MessagePlatformOutbox).where(MessagePlatformOutbox.session_id == SESSION_ID))).scalars().all())
                assert len(outbox_items) == 1
                assert outbox_items[0].source == "weixin-openclaw"
                assert outbox_items[0].event["type"] == "proactive_reply_error"
                assert outbox_items[0].event["content"] == error_message.content
                assert list((await db.execute(select(SessionEvent))).scalars().all()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
async def test_http_reply_work_status_exposes_the_persisted_rejection_message(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    async with database_factory(foreign_keys=True, wal=True) as factory:
        seeded = await _seed_work(
            factory,
            protocol=protocol,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        )
        _patch_queue_databases(monkeypatch, factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
        http = _FakeHttp(protocol, ["200"])
        _install_fake_http(monkeypatch, http)
        await consumer_module.SessionReplyConsumer()._run_claimed(seeded.work_id, WORKER_ID, 1, 2, "zh")
        _failed, error_message, _messages = await _persisted_failure(factory, seeded.work_id)

        app = _build_http_app(factory)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/chat/reply-works/{seeded.work_id}")
        assert response.status_code == 200
        payload = response.json()["data"]
        assert payload["status"] == SessionReplyWorkStatus.FAILED.value
        assert payload["error"] == error_message.content
        assert UPSTREAM_HTML not in payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
async def test_llm_request_size_limit_is_loaded_for_each_real_request(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    async with database_factory(foreign_keys=True, wal=True) as factory:
        async with factory() as db:
            db.add(SystemSetting(key="llm_request_max_size_mb", value="1"))
            await db.commit()
        monkeypatch.setattr(llm_request_policy_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(database_provider, "AsyncSessionLocal", factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
        http = _FakeHttp(protocol, ["200"])
        _install_fake_http(monkeypatch, http)
        messages = [InternalMessage(role=MessageRole.USER, content=LARGE_TEXT)]
        request_kwargs = {
            "api_key": "test-api-key",
            "base_url": "https://size-limit.invalid",
            "model_id": MODEL_ID,
            "messages": messages,
            "protocol": protocol,
            "channel_id": 1,
            "max_tokens": 512,
        }

        with pytest.raises(LLMRequestRejectedException) as rejection:
            await LLMClient.generate(**request_kwargs)
        assert rejection.value.source == "local"
        assert http.calls == []

        await _set_request_limit(factory, 2)
        response = await LLMClient.generate(**request_kwargs)
        assert response.message.role == MessageRole.ASSISTANT
        assert response.message.content == SUCCESS_CONTENT
        assert len(http.calls) == 1
        sent_data = http.calls[0]["kwargs"]["data"]
        assert isinstance(sent_data, bytes)
        parsed_payload = json.loads(sent_data)
        assert json.dumps(parsed_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") == sent_data
        assert 1024 * 1024 < len(sent_data) < 2 * 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_kind", "expected_endpoint"),
    [("json", "generations"), ("single_image", "edits"), ("multi_image", "edits")],
    ids=["json", "single-image", "multi-image"],
)
async def test_image_request_size_limit_is_loaded_for_each_real_request(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    request_kind: str,
    expected_endpoint: str,
) -> None:
    async with database_factory(foreign_keys=True, wal=True) as factory:
        async with factory() as db:
            db.add(SystemSetting(key="llm_request_max_size_mb", value="1"))
            await db.commit()
        monkeypatch.setattr(llm_request_policy_module, "AsyncSessionLocal", factory)

        prompt = LARGE_TEXT if request_kind == "json" else "edit this image"
        reference_images: list[tuple[str, bytes, str]] | None = None
        if request_kind == "single_image":
            reference_images = [("reference.png", b"x" * 1_100_000, "image/png")]
        elif request_kind == "multi_image":
            reference_images = [
                ("first.png", b"x" * 600_000, "image/png"),
                ("second.png", b"y" * 600_000, "image/png"),
            ]

        request_kwargs = {
            "api_key": "test-api-key",
            "base_url": "https://image.invalid",
            "model_id": MODEL_ID,
            "protocol": "openai_image",
            "prompt": prompt,
            "reference_images": reference_images,
        }
        http = _FakeImageHttp()
        _install_fake_image_http(monkeypatch, http)

        with pytest.raises(LLMRequestRejectedException) as rejection:
            await ImageGenerationClient.generate_image(**request_kwargs)
        assert rejection.value.source == "local"
        assert rejection.value.allow_channel_fallback is False
        assert http.session_calls == []
        assert http.connector_calls == []
        assert http.post_calls == []

        await _set_request_limit(factory, 2)
        response = await ImageGenerationClient.generate_image(**request_kwargs)
        assert response == {"data": []}
        assert len(http.session_calls) == 1
        assert len(http.connector_calls) == 1
        assert len(http.post_calls) == 1

        post_call = http.post_calls[0]
        assert post_call["url"] == f"https://image.invalid/images/{expected_endpoint}"
        assert post_call["kwargs"]["headers"]["Authorization"] == "Bearer test-api-key"
        if request_kind == "json":
            assert post_call["kwargs"]["headers"]["Content-Type"] == "application/json"
        else:
            assert "Content-Type" not in post_call["kwargs"]["headers"]

        data = post_call["kwargs"]["data"]
        if request_kind == "json":
            assert isinstance(data, bytes)
            body_size = len(data)
        else:
            assert not isinstance(data, bytes)
            assert data.size is not None
            writer = _CountingWriter()
            await data.write(writer)
            body_size = writer.size
            assert data.size == body_size
        assert 1024 * 1024 < body_size < 2 * 1024 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
async def test_upstream_413_falls_back_to_next_priority_and_succeeds_without_error_artifacts(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
) -> None:
    async with database_factory(foreign_keys=True, wal=True) as factory:
        seeded = await _seed_work(
            factory,
            protocol=protocol,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        )
        await _set_request_limit(factory, 2)
        _patch_queue_databases(monkeypatch, factory)
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
        http = _FakeHttp(protocol, ["413", "200"])
        _install_fake_http(monkeypatch, http)

        await consumer_module.SessionReplyConsumer()._run_claimed(seeded.work_id, WORKER_ID, 1, 2, "zh")
        async with factory() as db:
            work = await db.get(SessionReplyWorkItem, seeded.work_id)
            assert work is not None
            assert work.status == SessionReplyWorkStatus.SUCCEEDED
            messages = list((await db.execute(select(Message).where(Message.session_id == SESSION_ID).order_by(Message.id))).scalars().all())
            assert [message.role for message in messages if message.role == MessageRole.ERR] == []
            assistant_messages = [message for message in messages if message.role == MessageRole.ASSISTANT]
            assert len(assistant_messages) == 1
            assert assistant_messages[0].content == SUCCESS_CONTENT
            events = (await db.execute(select(SessionEvent).where(SessionEvent.session_id == SESSION_ID))).scalars().all()
            assert all(event.event["type"] != "proactive_reply_error" for event in events)

        assert len(http.calls) == 2
        assert http.calls[0]["url"].startswith("https://channel-1.invalid/")
        assert http.calls[1]["url"].startswith("https://channel-2.invalid/")
