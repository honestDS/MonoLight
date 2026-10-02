import base64
import copy
import io
import json
import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatcher as dispatcher_module
import app.core.tools.send_file_to_user as send_file_to_user_module
from app.api.v1 import files as files_module
from app.core.dispatchers import ChatDispatcher
from app.core.paths import get_user_temp_dir
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.user import User
from app.schemas.response import SentFile
from app.transformers.openai.responses import OpenAIResponsesTransformer
from tests.database_support import clone_sqlite_schema

UID = "generated-image-user"
PROFILE_ID = 1
CHANNEL_ID = 1
MODEL_ID = "image-reply-model"
USER_MESSAGE = "Generate a native image reply."
IMAGE_TEXT = "Here is the generated image."

_FORMAT_DETAILS = {
    "png": ("PNG", "image/png", "png"),
    "jpeg": ("JPEG", "image/jpeg", "jpg"),
    "webp": ("WEBP", "image/webp", "webp"),
}
_GENERATED_IMAGE_CASES = [
    pytest.param(
        False,
        with_text,
        output_format,
        "terminal-only",
        id=f"nonstream-{output_format}-{'text' if with_text else 'image'}",
    )
    for with_text in (False, True)
    for output_format in _FORMAT_DETAILS
] + [
    pytest.param(
        True,
        with_text,
        output_format,
        delivery,
        id=f"stream-{delivery}-{output_format}-{'text' if with_text else 'image'}",
    )
    for with_text in (False, True)
    for output_format in _FORMAT_DETAILS
    for delivery in ("done-terminal", "terminal-only")
]


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "generated-image-workflow.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    await clone_sqlite_schema(database_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _profile_configs() -> dict[str, Any]:
    return {
        "channel": {
            "chat_channel": {
                "chat_timeout": 60,
                "rules": [
                    {
                        "channel_id": CHANNEL_ID,
                        "model_id": MODEL_ID,
                        "priority": 1,
                        "weight": 1,
                    }
                ],
            }
        },
        "security": {"audit_threshold": 0},
        "tool": {"enabled_tools": []},
        "other": {"context_summary_threshold_percent": 90},
        "memory": {"enabled": False, "precheck_enabled": False},
    }


def _model_channel() -> ModelChannel:
    return ModelChannel(
        id=CHANNEL_ID,
        name="generated-image-test-channel",
        api_key="enc:v1:stored-test-key",
        base_url="https://llm.invalid",
        model_ids=[
            {
                "model_id": MODEL_ID,
                "usage": "CHAT",
                "protocol": "OPENAI_RESPONSES",
                "context_window_k": 32,
                "max_tokens": 512,
                "temperature": 0,
                "top_p": 1,
            }
        ],
    )


def _patch_runtime_database(monkeypatch: pytest.MonkeyPatch, factory: async_sessionmaker[AsyncSession]) -> None:
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", factory)
    monkeypatch.setattr(send_file_to_user_module, "_get_encryption_key", lambda: b"g" * 32)


async def _seed_conversation(factory: async_sessionmaker[AsyncSession], session_id: str) -> InternalMessage:
    async with factory() as db:
        db.add(_model_channel())
        db.add(
            Profile(
                id=PROFILE_ID,
                uid=UID,
                name=f"generated-image-profile-{session_id}",
                configs=_profile_configs(),
            )
        )
        db.add(User(uid=UID, username="generated_image_user"))
        await db.flush()
        db.add(
            ChatSession(
                session_id=session_id,
                uid=UID,
                profile_id=PROFILE_ID,
                goal_mode=False,
                max_turns=5,
                source="http",
            )
        )
        await db.flush()
        initial_message = Message(
            session_id=session_id,
            uid=UID,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content=USER_MESSAGE,
            profile_id=PROFILE_ID,
            is_processed=False,
        )
        db.add(initial_message)
        await db.commit()
        await db.refresh(initial_message)
        assert initial_message.id is not None
        return InternalMessage(
            id=initial_message.id,
            role=MessageRole.USER,
            content=initial_message.content,
            created_at=initial_message.created_at.timestamp(),
        )


def _image_fixture(output_format: str) -> tuple[bytes, str]:
    image = Image.new("RGB", (2, 2), color=(32, 128, 224))
    buffer = io.BytesIO()
    try:
        image.save(buffer, format=_FORMAT_DETAILS[output_format][0])
    finally:
        image.close()
    raw = buffer.getvalue()
    return raw, base64.b64encode(raw).decode("ascii")


def _raw_response(*, image_id: str, image_data: str, output_format: str, with_text: bool) -> dict[str, Any]:
    image_item = {
        "type": "image_generation_call",
        "status": "completed",
        "id": image_id,
        "result": image_data,
        "output_format": output_format,
        "metadata": {"provider_only": "opaque"},
    }
    output: list[dict[str, Any]] = []
    if with_text:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": IMAGE_TEXT}],
            }
        )
    output.append(image_item)
    return {
        "id": f"resp-{image_id}",
        "object": "response",
        "status": "completed",
        "model": MODEL_ID,
        "output": output,
        "usage": {"input_tokens": 9, "output_tokens": 7, "total_tokens": 16},
        "metadata": {"provider_only": "opaque"},
    }


def _stream_events(raw_response: dict[str, Any], *, with_text: bool, delivery: str) -> list[dict[str, Any]]:
    image_item = raw_response["output"][-1]
    events: list[dict[str, Any]] = []
    if with_text:
        events.append(
            {
                "type": "response.output_text.delta",
                "delta": IMAGE_TEXT,
                "output_index": 0,
                "content_index": 0,
            }
        )
    if delivery == "done-terminal":
        events.append(
            {
                "type": "response.output_item.done",
                "output_index": 1 if with_text else 0,
                "item": image_item,
            }
        )
    events.append({"type": "response.completed", "response": copy.deepcopy(raw_response)})
    return events


def _patch_responses_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_response: dict[str, Any],
    stream: bool,
    stream_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    if stream:

        async def fake_stream(_transformer, **kwargs: Any):
            calls.append(kwargs)
            normalize_event = kwargs["normalize_event"]
            for official_event in stream_events:
                normalized, _has_payload = normalize_event(official_event)
                if normalized is not None:
                    yield normalized

        monkeypatch.setattr(OpenAIResponsesTransformer, "_stream_sse_json", fake_stream)
    else:

        async def fake_post(_transformer, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return copy.deepcopy(raw_response)

        monkeypatch.setattr(OpenAIResponsesTransformer, "_post_json", fake_post)
    return calls


def _assert_file_entries(
    files: list[dict[str, Any]],
    *,
    expected_bytes: bytes,
    expected_dir: Path,
    expected_mime_type: str,
    expected_suffix: str,
) -> dict[str, Any]:
    assert len(files) == 1
    file_entry = files[0]
    assert file_entry["mime_type"] == expected_mime_type
    assert file_entry["previewable"] is True
    assert file_entry["size"] == len(expected_bytes)
    assert file_entry["name"].endswith(f".{expected_suffix}")
    resolved_path = send_file_to_user_module.resolve_file_token(file_entry["id"])
    assert resolved_path.parent == expected_dir.resolve()
    assert resolved_path.read_bytes() == expected_bytes
    return file_entry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream", "with_text", "output_format", "stream_image_delivery"),
    _GENERATED_IMAGE_CASES,
)
async def test_native_generated_image_reply_round_trips_through_dispatch_persistence_and_download(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    with_text: bool,
    output_format: str,
    stream_image_delivery: str,
) -> None:
    session_id = f"generated-image-{output_format}-{'text' if with_text else 'image'}-{'stream' if stream else 'nonstream'}-{stream_image_delivery}"
    expected_bytes, image_data = _image_fixture(output_format)
    raw_response = _raw_response(
        image_id=f"image-{output_format}-{'text' if with_text else 'image'}",
        image_data=image_data,
        output_format=output_format,
        with_text=with_text,
    )
    network_calls = _patch_responses_transport(
        monkeypatch,
        raw_response=raw_response,
        stream=stream,
        stream_events=_stream_events(raw_response, with_text=with_text, delivery=stream_image_delivery),
    )
    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.chdir(tmp_path)

    events: list[dict[str, Any]] = []
    turn_end_payloads: list[dict[str, Any]] = []
    expected_dir = get_user_temp_dir(os.getcwd(), session_id) / "generated_images"
    expected_mime_type = _FORMAT_DETAILS[output_format][1]
    expected_suffix = _FORMAT_DETAILS[output_format][2]

    async def capture(event_payload: dict[str, Any]) -> None:
        events.append(event_payload)
        if event_payload.get("type") != "turn_end":
            return
        content = event_payload.get("content")
        assert isinstance(content, str)
        assistant_files = json.loads(content)
        assert assistant_files["type"] == "assistant_files"
        _assert_file_entries(
            assistant_files["files"],
            expected_bytes=expected_bytes,
            expected_dir=expected_dir,
            expected_mime_type=expected_mime_type,
            expected_suffix=expected_suffix,
        )
        turn_end_payloads.append(assistant_files)

    initial_message = await _seed_conversation(session_factory, session_id)
    async with session_factory() as db:
        response = await ChatDispatcher.dispatch(
            db,
            message=USER_MESSAGE,
            uid=UID,
            session_id=session_id,
            persisted_initial_message=initial_message,
            persisted_profile_id=PROFILE_ID,
            frozen_user_message_ids=[initial_message.id],
            history_before_id=initial_message.id,
            session_source="http",
            stream_event_callback=capture if stream else None,
        )

    assert len(network_calls) == 1
    assert network_calls[0]["url"] == "https://llm.invalid/responses"
    assert network_calls[0]["payload"]["model"] == MODEL_ID
    assert network_calls[0]["payload"]["stream"] is stream

    response_files = response["files"]
    assert isinstance(response_files, list)
    response_file = _assert_file_entries(
        response_files,
        expected_bytes=expected_bytes,
        expected_dir=expected_dir,
        expected_mime_type=expected_mime_type,
        expected_suffix=expected_suffix,
    )
    choice_message = response["choices"][0]["message"]
    assert response["choices"][0]["finish_reason"] == "stop"
    assert isinstance(choice_message["content"], str)
    response_content = json.loads(choice_message["content"])
    assert response_content["type"] == "assistant_files"
    assert response_content["text"] == (IMAGE_TEXT if with_text else "")
    assert [SentFile.model_validate(file_entry) for file_entry in response_content["files"]] == [SentFile.model_validate(file_entry) for file_entry in response_files]
    assert response["history"][-1]["content"] == choice_message["content"]
    assert response["history"][-1]["role"] == MessageRole.ASSISTANT

    if stream:
        assert len(turn_end_payloads) == 1
        assert turn_end_payloads[0] == response_content
    else:
        assert events == []

    generated_dir_files = [path for path in expected_dir.iterdir() if path.is_file() and not path.name.startswith(".")]
    assert len(generated_dir_files) == 1
    assert generated_dir_files[0].read_bytes() == expected_bytes

    async with session_factory() as db:
        result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))
        messages = list(result.scalars().all())
        assistant_messages = [message for message in messages if message.role == MessageRole.ASSISTANT]
        assert len(assistant_messages) == 1
        assistant_message = assistant_messages[0]
        await db.refresh(assistant_message)

    assert assistant_message.type == MessageType.TEXT
    assert isinstance(assistant_message.content, str)
    assert json.loads(assistant_message.content) == response_content
    assert image_data not in assistant_message.content
    serialized_observations = json.dumps({"response": response, "events": events}, ensure_ascii=False)
    assert image_data not in serialized_observations
    assert image_data not in json.dumps(response_content, ensure_ascii=False)

    app = FastAPI()
    app.include_router(files_module.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        download_response = await client.get(response_file["download_url"])
    assert download_response.status_code == 200
    assert download_response.content == expected_bytes
    assert download_response.headers["content-type"].split(";", 1)[0] == expected_mime_type
