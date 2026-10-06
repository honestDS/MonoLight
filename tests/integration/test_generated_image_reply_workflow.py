import base64
import copy
import io
import json
import os
from collections.abc import AsyncGenerator
from email import policy
from email.parser import BytesParser
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

import app.core.background_tasks.runner as runner_module
import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatcher as dispatcher_module
import app.core.dispatchers.background as background_dispatcher_module
import app.core.tools.send_file_to_user as send_file_to_user_module
import app.tasks as tasks_module
import app.transformers.openai.image_generation as image_generation_transformer_module
from app.api.v1 import files as files_module
from app.core.dispatchers import ChatDispatcher
from app.core.paths import get_user_temp_dir
from app.core.security import get_current_user
from app.core.tools import get_tools_for_profile
from app.core.tools.image_generation import ImageGenerationExecutor
from app.core.utils.background_task_result import build_background_task_success_result
from app.core.utils.dispatcher.process_single_tool import process_single_tool
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.handler import register_handlers
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.user import User
from app.providers.database import get_db
from app.providers.image_generation import ImageGenerationClient
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
_GENERATE_IMAGE_CASES = [
    pytest.param(
        with_reference,
        output_format,
        id=f"openai-image-{'reference' if with_reference else 'no-reference'}-{output_format}",
    )
    for with_reference in (False, True)
    for output_format in _FORMAT_DETAILS
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


def _image_fixture(output_format: str, *, color: tuple[int, int, int] = (32, 128, 224)) -> tuple[bytes, str]:
    image = Image.new("RGB", (2, 2), color=color)
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


async def _serialize_multipart_form(form: Any) -> bytes:
    chunks = bytearray()

    class _Writer:
        async def write(self, data: bytes) -> None:
            chunks.extend(data)

        async def write_eof(self) -> None:
            return None

        async def drain(self) -> None:
            return None

    await form.write(_Writer())
    return bytes(chunks)


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
    expected_dir = get_user_temp_dir(os.getcwd(), UID) / "generated_images"
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
    assert not get_user_temp_dir(os.getcwd(), session_id).exists()

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("with_reference", "output_format"),
    _GENERATE_IMAGE_CASES,
)
async def test_generate_image_background_workflow_supports_openai_images_and_reference_edits(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_reference: bool,
    output_format: str,
) -> None:
    session_id = f"generated-image-openai-image-{'reference' if with_reference else 'generation'}-{output_format}"
    expected_bytes, image_data = _image_fixture(output_format)
    reference_bytes, _ = _image_fixture("png", color=(224, 64, 32))
    image_model_id = f"image-openai-image-{output_format}"
    prompt = f"Generate a {output_format} test image through openai-image."
    initial_message = await _seed_conversation(session_factory, session_id)

    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.setattr(runner_module, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(files_module, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.chdir(tmp_path)

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as db:
            yield db

    async def override_get_current_user() -> User:
        return User(uid=UID, username="generated_image_user")

    files_app = FastAPI()
    files_app.include_router(files_module.router, prefix="/api/v1")
    files_app.dependency_overrides[get_db] = override_get_db
    files_app.dependency_overrides[get_current_user] = override_get_current_user

    reference_path: Path | None = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=files_app), base_url="http://test") as files_client:
        if with_reference:
            upload_response = await files_client.post(
                "/api/v1/upload",
                files={"file": ("reference.png", reference_bytes, "image/png")},
                data={"session_id": session_id},
            )
            assert upload_response.status_code == 200
            reference_path = Path(upload_response.json()["path"])
            assert reference_path.parent == get_user_temp_dir(os.getcwd(), UID).resolve()
            assert reference_path.read_bytes() == reference_bytes

        async with session_factory() as db:
            profile = await db.get(Profile, PROFILE_ID)
            channel = await db.get(ModelChannel, CHANNEL_ID)
            assert profile is not None
            assert channel is not None

            profile_configs = copy.deepcopy(profile.configs)
            profile_configs["channel"]["image_generation_channel"] = {
                "rules": [
                    {
                        "channel_id": CHANNEL_ID,
                        "model_id": image_model_id,
                        "priority": 1,
                        "weight": 1,
                    }
                ]
            }
            profile_configs["tool"]["enabled_tools"] = ["generate_image", "send_file_to_user"]
            profile_configs["tool"]["allowed_operation_dirs"] = [str(tmp_path.resolve())]
            profile.configs = profile_configs
            channel.base_url = "https://llm.invalid/v1"
            channel.model_ids = [
                *channel.model_ids,
                {
                    "model_id": image_model_id,
                    "usage": "IMAGE_GENERATION",
                    "protocol": "OPENAI_IMAGE",
                    "size": "1024x1024",
                    "quality": "auto",
                },
            ]
            await db.commit()
            await db.refresh(profile)
            await db.refresh(channel)

            cfg = ProfileConfig.model_validate(profile.configs)
            tools, _ = await get_tools_for_profile(db, profile)

            tool_by_name = {tool["function"]["name"]: tool for tool in tools if isinstance(tool, dict) and isinstance(tool.get("function"), dict)}
            assert {"generate_image", "send_file_to_user"}.issubset(tool_by_name)
            assert "reference_images" in tool_by_name["generate_image"]["function"]["parameters"]["properties"]
            assert any(item.get("usage") == "CHAT" and item.get("model_id") == MODEL_ID for item in channel.model_ids)
            assert any(item.get("usage") == "IMAGE_GENERATION" and item.get("model_id") == image_model_id and item.get("protocol") == "OPENAI_IMAGE" for item in channel.model_ids)

            arguments: dict[str, Any] = {"prompt": prompt}
            if reference_path is not None:
                arguments["reference_images"] = [str(reference_path)]

            tool_message = await process_single_tool(
                InternalToolCall(
                    id=f"generate-image-openai-image-{output_format}",
                    name="generate_image",
                    arguments=arguments,
                ),
                db,
                profile,
                cfg,
                [initial_message],
                "generated_image_user",
                session_id,
                1,
                UID,
                source_message_id=initial_message.id,
            )

        queued_payload = json.loads(tool_message.content or "{}")
        assert queued_payload["status"] == "queued"
        task_id = queued_payload["task_id"]
        assert isinstance(task_id, int)

        async with session_factory() as db:
            queued_task = await db.get(BackgroundTask, task_id)
            assert queued_task is not None
            assert queued_task.status == BackgroundTaskStatus.PENDING
            assert queued_task.arguments == arguments
            assert set(queued_task.arguments).issubset({"prompt", "reference_images"})
            assert "protocol" not in queued_task.arguments
            assert "mask" not in queued_task.arguments

        image_http_calls: list[dict[str, Any]] = []

        class _FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc_value, _traceback):
                return None

            async def text(self) -> str:
                return json.dumps({"created": 1, "data": [{"b64_json": image_data}]})

        class _FakeClientSession:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                image_http_calls.append({"session_args": args, "session_kwargs": kwargs})

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc_value, _traceback):
                return None

            def post(self, url: str, **kwargs: Any) -> _FakeResponse:
                image_http_calls.append({"url": url, "kwargs": kwargs})
                return _FakeResponse()

        monkeypatch.setattr(image_generation_transformer_module.aiohttp, "ClientSession", _FakeClientSession)
        monkeypatch.setattr(image_generation_transformer_module.aiohttp, "TCPConnector", lambda **_kwargs: object())

        await runner_module.run_background_task(task_id, worker_id=f"image-openai-image-{output_format}")

        async with session_factory() as db:
            completed_task = await db.get(BackgroundTask, task_id)
            assert completed_task is not None
            assert completed_task.status == BackgroundTaskStatus.SUCCEEDED
            assert completed_task.result is not None
            task_result = copy.deepcopy(completed_task.result)

        assert image_data not in json.dumps(task_result, ensure_ascii=False)
        assert task_result["status"] == "succeeded"
        assert task_result["tool_name"] == "generate_image"
        result_content = task_result["content"]
        send_arguments = result_content["send_file_to_user"]
        assert isinstance(send_arguments["files"], list)
        assert len(send_arguments["files"]) == 1
        generated_file = send_arguments["files"][0]
        generated_path = Path(generated_file["path"])
        assert generated_path.read_bytes() == expected_bytes
        assert generated_file["mime_type"] == _FORMAT_DETAILS[output_format][1]
        assert generated_path.suffix == f".{_FORMAT_DETAILS[output_format][2]}"

        send_executor = send_file_to_user_module.SendFileToUserExecutor(project_root=os.getcwd(), uid=UID)
        send_executor.set_config(cfg)
        sent_payload = json.loads(await send_executor.execute(**send_arguments))
        assert sent_payload["status"] == "success"
        sent_file = _assert_file_entries(
            sent_payload["files"],
            expected_bytes=expected_bytes,
            expected_dir=get_user_temp_dir(os.getcwd(), UID) / "generated_images",
            expected_mime_type=_FORMAT_DETAILS[output_format][1],
            expected_suffix=_FORMAT_DETAILS[output_format][2],
        )

        download_response = await files_client.get(sent_file["download_url"])
        assert download_response.status_code == 200
        assert download_response.content == expected_bytes
        assert download_response.headers["content-type"].split(";", 1)[0] == _FORMAT_DETAILS[output_format][1]

        assert len(image_http_calls) == 2
        request = image_http_calls[1]
        assert request["url"] == f"https://llm.invalid/v1/images/{'edits' if with_reference else 'generations'}"
        if with_reference:
            form = request["kwargs"].get("data")
            assert form is not None
            multipart = form()
            content_type = multipart.headers["Content-Type"]
            body = await _serialize_multipart_form(multipart)
            message = BytesParser(policy=policy.default).parsebytes(f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n".encode() + body)
            assert message.is_multipart()
            fields: dict[str, str] = {}
            file_parts: list[tuple[str | None, str | None, str, bytes | None]] = []
            for part in message.iter_parts():
                field_name = part.get_param("name", header="content-disposition")
                field_data = part.get_payload(decode=True)
                if part.get_filename() is not None:
                    file_parts.append((field_name, part.get_filename(), part.get_content_type(), field_data))
                else:
                    assert field_name is not None
                    assert field_data is not None
                    fields[field_name] = field_data.decode()
            assert fields == {
                "model": image_model_id,
                "prompt": prompt,
                "n": "1",
                "size": "1024x1024",
                "quality": "auto",
            }
            assert len(file_parts) == 1
            field_name, filename, mime_type, file_data = file_parts[0]
            assert field_name == "image"
            assert filename is not None and filename.endswith(".png")
            assert mime_type == "image/png"
            assert file_data == reference_bytes
            assert "mask" not in fields
        else:
            payload = request["kwargs"].get("json")
            assert payload == {
                "model": image_model_id,
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024",
                "quality": "auto",
            }
            assert "mask" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_image_count", (1, 2))
async def test_completed_background_image_tasks_do_not_regenerate_images_in_final_reply(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested_image_count: int,
) -> None:
    session_id = f"background-generated-images-{requested_image_count}"
    request_text = f"Generate exactly {requested_image_count} native image{'s' if requested_image_count != 1 else ''}."
    expected_bytes, expected_image_data = _image_fixture("png")
    _, red_image_data = _image_fixture("png", color=(224, 32, 32))
    image_generation_calls: list[dict[str, Any]] = []
    gateway_requests: list[dict[str, Any]] = []
    gateway_responses: list[dict[str, Any]] = []
    merged_files: list[dict[str, Any]] = []

    async def fake_generate_image(**kwargs: Any) -> dict[str, Any]:
        image_generation_calls.append(copy.deepcopy(kwargs))
        return {
            "created": 1,
            "data": [{"b64_json": expected_image_data}],
            "model": "background-image-model",
        }

    async def fake_post(_transformer, **kwargs: Any) -> dict[str, Any]:
        payload = copy.deepcopy(kwargs["payload"])
        gateway_requests.append(payload)
        input_items = payload.get("input") if isinstance(payload.get("input"), list) else []
        has_send_file_call = any(isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == "send_file_to_user" for item in input_items)
        if not has_send_file_call:
            output = [
                {
                    "type": "function_call",
                    "id": "fc-send-background-files",
                    "call_id": "call-send-background-files",
                    "name": "send_file_to_user",
                    "arguments": json.dumps({"files": merged_files}, ensure_ascii=False),
                    "status": "completed",
                }
            ]
        else:
            output = [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "The completed background images are ready."}],
                }
            ]
            if payload.get("tool_choice") != "none":
                output.append(
                    {
                        "type": "image_generation_call",
                        "status": "completed",
                        "id": "native-red-extra-image",
                        "result": red_image_data,
                        "output_format": "png",
                    }
                )
        response = {
            "id": f"background-response-{len(gateway_responses) + 1}",
            "object": "response",
            "status": "completed",
            "model": MODEL_ID,
            "output": output,
            "usage": {"input_tokens": 19, "output_tokens": 11, "total_tokens": 30},
            "tools": [{"type": "image_generation"}],
        }
        gateway_responses.append(copy.deepcopy(response))
        return copy.deepcopy(response)

    monkeypatch.setattr(ImageGenerationClient, "generate_image", fake_generate_image)
    monkeypatch.setattr(OpenAIResponsesTransformer, "_post_json", fake_post)
    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.setattr(background_dispatcher_module, "AsyncSessionLocal", session_factory)
    monkeypatch.chdir(tmp_path)

    initial_message = await _seed_conversation(session_factory, session_id)
    task_ids: list[int] = []
    async with session_factory() as db:
        user_message = await db.get(Message, initial_message.id)
        profile = await db.get(Profile, PROFILE_ID)
        channel = await db.get(ModelChannel, CHANNEL_ID)
        assert user_message is not None
        assert profile is not None
        assert channel is not None
        user_message.content = request_text
        user_message.is_processed = True
        profile_configs = copy.deepcopy(profile.configs)
        profile_configs["channel"]["image_generation_channel"] = {
            "rules": [
                {
                    "channel_id": CHANNEL_ID,
                    "model_id": "background-image-model",
                    "priority": 1,
                    "weight": 1,
                }
            ]
        }
        profile_configs["tool"]["enabled_tools"] = ["generate_image", "send_file_to_user"]
        profile_configs["tool"]["allowed_operation_dirs"] = [str(tmp_path.resolve())]
        profile.configs = profile_configs
        channel.model_ids = [
            *channel.model_ids,
            {
                "model_id": "background-image-model",
                "usage": "IMAGE_GENERATION",
                "protocol": "OPENAI_IMAGE",
                "size": "1024x1024",
                "quality": "auto",
            },
        ]
        await db.commit()
        await db.refresh(profile)

        cfg = ProfileConfig.model_validate(profile.configs)
        for index in range(requested_image_count):
            tool_call_id = f"generate-image-{requested_image_count}-{index + 1}"
            arguments = {
                "prompt": f"Background image {index + 1}",
                "size": "1024x1024",
                "quality": "auto",
            }
            executor = ImageGenerationExecutor(project_root=os.getcwd(), uid=UID)
            executor.set_config(cfg)
            executor.set_runtime_context(
                db=db,
                profile=profile,
                session_id=session_id,
                source_message_id=initial_message.id,
            )
            raw_result = await executor.execute(**arguments)
            parsed_result = json.loads(raw_result)
            assert parsed_result["status"] == "success"
            successful_files = parsed_result["send_file_to_user"]["files"]
            assert isinstance(successful_files, list)
            merged_files.extend(copy.deepcopy(successful_files))

            assistant_tool_call = InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(
                        id=tool_call_id,
                        name="generate_image",
                        arguments=arguments,
                    )
                ],
            )
            await save_assistant_message(
                db,
                session_id,
                UID,
                PROFILE_ID,
                assistant_tool_call,
            )
            tool_response = InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id=tool_call_id,
                content=raw_result,
            )
            await save_tool_response(
                db,
                session_id,
                UID,
                PROFILE_ID,
                tool_response,
                [],
                [],
            )
            task = BackgroundTask(
                uid=UID,
                session_id=session_id,
                profile_id=PROFILE_ID,
                tool_call_id=tool_call_id,
                tool_name="generate_image",
                status=BackgroundTaskStatus.SUCCEEDED,
                arguments=arguments,
                result=build_background_task_success_result("generate_image", parsed_result),
                attempt_count=1,
                auto_reply=True,
                reply_status=BackgroundTaskReplyStatus.PENDING,
            )
            db.add(task)
            await db.flush()
            assert task.id is not None
            task_ids.append(task.id)
        await db.commit()

    assert len(merged_files) == requested_image_count

    dispatch_result = await ChatDispatcher.dispatch_proactive_reply(task_ids[-1])

    assert len(image_generation_calls) == requested_image_count
    assert all(call["n"] == 1 for call in image_generation_calls)
    assert all(call["model_id"] == "background-image-model" for call in image_generation_calls)
    assert len(gateway_requests) == 2
    assert gateway_requests[0]["tools"]
    assert gateway_requests[0]["tool_choice"] == "auto"
    assert "tools" not in gateway_requests[1]
    assert gateway_requests[1].get("tool_choice") == "none"
    assert not any(isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == "send_file_to_user" for item in gateway_requests[0].get("input", []))
    assert any(isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") == "send_file_to_user" for item in gateway_requests[1].get("input", []))
    assert len(gateway_responses) == 2
    for response in gateway_responses:
        assert response["tools"] == [{"type": "image_generation"}]
        assert [item for item in response["output"] if item.get("type") == "image_generation_call"] == []

    returned_files = dispatch_result["files"]
    assert isinstance(returned_files, list)
    assert len(returned_files) == requested_image_count

    async with session_factory() as db:
        message_result = await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.asc()))
        messages = list(message_result.scalars().all())
        task_result = await db.execute(select(BackgroundTask).where(BackgroundTask.session_id == session_id).order_by(BackgroundTask.id.asc()))
        tasks = list(task_result.scalars().all())

    assert len(tasks) == requested_image_count
    assert all(task.status == BackgroundTaskStatus.SUCCEEDED for task in tasks)
    assert all(task.attempt_count == 1 for task in tasks)
    assert all(task.tool_name == "generate_image" for task in tasks)
    assert all(task.uid == UID and task.profile_id == PROFILE_ID and task.tool_call_id for task in tasks)

    send_file_results: list[dict[str, Any]] = []
    assistant_files_payloads: list[dict[str, Any]] = []
    for message in messages:
        try:
            content = InternalMessage.model_validate_json(message.content or "{}").content if message.type == MessageType.TOOL_RESULT else message.content
            payload = json.loads(content or "{}")
        except (TypeError, ValueError):
            continue
        if message.role == MessageRole.TOOL and payload.get("type") == "files_to_user":
            send_file_results.append(payload)
        if message.role == MessageRole.ASSISTANT and payload.get("type") == "assistant_files":
            assistant_files_payloads.append(payload)

    assert len(send_file_results) == 1
    sent_files = send_file_results[0]["files"]
    assert len(sent_files) == requested_image_count
    assert [file_entry["id"] for file_entry in returned_files] == [file_entry["id"] for file_entry in sent_files]
    assert len(assistant_files_payloads) == 1
    assert len(assistant_files_payloads[0]["files"]) == requested_image_count
    assert [file_entry["id"] for file_entry in assistant_files_payloads[0]["files"]] == [file_entry["id"] for file_entry in sent_files]

    expected_dir = get_user_temp_dir(os.getcwd(), UID) / "generated_images"
    for file_entry in sent_files:
        _assert_file_entries(
            [file_entry],
            expected_bytes=expected_bytes,
            expected_dir=expected_dir,
            expected_mime_type="image/png",
            expected_suffix="png",
        )

    temp_root = tmp_path / "temp"
    generated_files = [path for path in temp_root.rglob("*") if path.is_file() and not path.name.startswith(".")]
    assert len(generated_files) == requested_image_count
    assert all(path.read_bytes() == expected_bytes for path in generated_files)

    app = FastAPI()
    app.include_router(files_module.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for file_entry in sent_files:
            download_response = await client.get(file_entry["download_url"])
            assert download_response.status_code == 200
            assert download_response.content == expected_bytes
            assert download_response.headers["content-type"].split(";", 1)[0] == "image/png"


@pytest.mark.asyncio
async def test_mixed_image_sources_share_per_user_temp_size_quota(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_a = "mixed-image-quota-session-a"
    session_b = "mixed-image-quota-session-b"
    other_uid = "generated-image-other-user"
    expected_bytes, image_data = _image_fixture("png")
    raw_response = _raw_response(
        image_id="mixed-native-image",
        image_data=image_data,
        output_format="png",
        with_text=False,
    )
    network_calls = _patch_responses_transport(
        monkeypatch,
        raw_response=raw_response,
        stream=False,
        stream_events=[],
    )
    _patch_runtime_database(monkeypatch, session_factory)
    monkeypatch.chdir(tmp_path)

    temp_root = tmp_path / "temp"
    monkeypatch.setattr(files_module, "TEMP_DIR", temp_root)
    monkeypatch.setattr(tasks_module, "TEMP_DIR", temp_root)
    monkeypatch.setattr(runner_module, "AsyncSessionLocal", session_factory)

    initial_message = await _seed_conversation(session_factory, session_a)
    async with session_factory() as db:
        db.add(
            ChatSession(
                session_id=session_b,
                uid=UID,
                profile_id=PROFILE_ID,
                goal_mode=False,
                max_turns=5,
                source="http",
            )
        )
        await db.commit()

    async with session_factory() as db:
        response = await ChatDispatcher.dispatch(
            db,
            message=USER_MESSAGE,
            uid=UID,
            session_id=session_a,
            persisted_initial_message=initial_message,
            persisted_profile_id=PROFILE_ID,
            frozen_user_message_ids=[initial_message.id],
            history_before_id=initial_message.id,
            session_source="http",
        )

    assert len(network_calls) == 1
    native_files = response["files"]
    assert isinstance(native_files, list)
    user_dir = get_user_temp_dir(os.getcwd(), UID)
    generated_dir = user_dir / "generated_images"
    native_file = _assert_file_entries(
        native_files,
        expected_bytes=expected_bytes,
        expected_dir=generated_dir,
        expected_mime_type="image/png",
        expected_suffix="png",
    )
    native_path = send_file_to_user_module.resolve_file_token(native_file["id"])

    async def fake_generate_image(**_kwargs: Any) -> dict[str, Any]:
        return {
            "created": 1,
            "data": [{"b64_json": image_data}],
            "model": "background-image-model",
        }

    monkeypatch.setattr(ImageGenerationClient, "generate_image", fake_generate_image)

    task_arguments = {
        "prompt": "Background quota image",
        "size": "1024x1024",
        "quality": "auto",
    }
    async with session_factory() as db:
        profile = await db.get(Profile, PROFILE_ID)
        channel = await db.get(ModelChannel, CHANNEL_ID)
        assert profile is not None
        assert channel is not None
        profile_configs = copy.deepcopy(profile.configs)
        profile_configs["channel"]["image_generation_channel"] = {
            "rules": [
                {
                    "channel_id": CHANNEL_ID,
                    "model_id": "background-image-model",
                    "priority": 1,
                    "weight": 1,
                }
            ]
        }
        profile_configs["tool"]["enabled_tools"] = ["generate_image"]
        profile_configs["tool"]["allowed_operation_dirs"] = [str(tmp_path.resolve())]
        profile.configs = profile_configs
        channel.model_ids = [
            *channel.model_ids,
            {
                "model_id": "background-image-model",
                "usage": "IMAGE_GENERATION",
                "protocol": "OPENAI_IMAGE",
                "size": "1024x1024",
                "quality": "auto",
            },
        ]
        task = BackgroundTask(
            uid=UID,
            session_id=session_b,
            profile_id=PROFILE_ID,
            tool_call_id="mixed-quota-generate-image",
            tool_name="generate_image",
            status=BackgroundTaskStatus.PENDING,
            arguments=task_arguments,
            auto_reply=False,
            reply_status=BackgroundTaskReplyStatus.NONE,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        assert task.id is not None
        task_id = task.id

    await runner_module.run_background_task(task_id, worker_id="mixed-quota-worker")

    async with session_factory() as db:
        completed_task = await db.get(BackgroundTask, task_id)
        assert completed_task is not None
        assert completed_task.status == BackgroundTaskStatus.SUCCEEDED
        assert completed_task.attempt_count == 1
        assert completed_task.result is not None
        background_path = Path(completed_task.result["content"]["send_file_to_user"]["files"][0]["path"])

    assert background_path.parent == generated_dir.resolve()
    assert background_path.read_bytes() == expected_bytes
    assert background_path != native_path

    deep_result_path = user_dir / "tool_results" / "deep" / "result.bin"
    deep_result_path.parent.mkdir(parents=True, exist_ok=True)
    deep_result_path.write_bytes(expected_bytes)

    upload_uid = UID

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as db:
            yield db

    async def override_get_current_user() -> User:
        return User(uid=upload_uid, username=f"{upload_uid}-user")

    app = FastAPI()
    register_handlers(app)
    app.include_router(files_module.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        async def upload_fixture(session_id: str | None) -> Path:
            request_data = {} if session_id is None else {"session_id": session_id}
            upload_response = await client.post(
                "/api/v1/upload",
                files={"file": ("fixture.png", expected_bytes, "image/png")},
                data=request_data,
            )
            assert upload_response.status_code == 200
            upload_path = Path(upload_response.json()["path"])
            expected_upload_dir = get_user_temp_dir(os.getcwd(), upload_uid)
            assert upload_path.parent == expected_upload_dir.resolve()
            assert upload_path.read_bytes() == expected_bytes
            return upload_path

        upload_paths = [await upload_fixture(session_a), await upload_fixture(session_b)]
        upload_uid = other_uid
        other_upload_paths = [await upload_fixture(None), await upload_fixture(None)]

        assert all(path.parent == user_dir.resolve() for path in upload_paths)
        other_user_dir = get_user_temp_dir(os.getcwd(), other_uid)
        assert all(path.parent == other_user_dir.resolve() for path in other_upload_paths)
        assert len([path for path in generated_dir.iterdir() if path.is_file()]) == 2
        assert len([path for path in user_dir.iterdir() if path.is_file()]) == 2
        assert deep_result_path.read_bytes() == expected_bytes

        temp_entries = list(temp_root.iterdir())
        assert all(path.is_dir() for path in temp_entries)
        assert {path.name for path in temp_entries} == {user_dir.name, other_user_dir.name}
        assert not get_user_temp_dir(os.getcwd(), session_a).exists()
        assert not get_user_temp_dir(os.getcwd(), session_b).exists()

        first_user_files = [native_path, background_path, *upload_paths, deep_result_path]
        assert len(first_user_files) == 5
        assert all(path.stat().st_size == len(expected_bytes) for path in first_user_files)
        assert sum(path.stat().st_size for path in generated_dir.iterdir() if path.is_file()) == 2 * len(expected_bytes)
        assert sum(path.stat().st_size for path in user_dir.iterdir() if path.is_file()) == 2 * len(expected_bytes)
        assert deep_result_path.stat().st_size == len(expected_bytes)
        assert {path.resolve() for path in user_dir.rglob("*") if path.is_file()} == {path.resolve() for path in first_user_files}

        for index, path in enumerate(first_user_files):
            timestamp = 1_000_000.0 + index
            os.utime(path, (timestamp, timestamp))

        native_download_before = await client.get(native_file["download_url"])
        assert native_download_before.status_code == 200
        assert native_download_before.content == expected_bytes

        max_size_bytes = 4 * len(expected_bytes)
        deleted_count, current_size = tasks_module._cleanup_temp_dir_by_size(max_size_bytes)
        assert deleted_count == 1
        assert current_size == max_size_bytes
        assert not native_path.exists()
        assert all(path.exists() for path in [background_path, *upload_paths, deep_result_path])
        assert all(path.exists() for path in other_upload_paths)

        remaining_first_user_files = [path for path in user_dir.rglob("*") if path.is_file()]
        other_user_files = [path for path in other_user_dir.rglob("*") if path.is_file()]
        assert len(remaining_first_user_files) == 4
        assert sum(path.stat().st_size for path in remaining_first_user_files) == max_size_bytes
        assert len(other_user_files) == 2
        assert sum(path.stat().st_size for path in other_user_files) == 2 * len(expected_bytes)
        project_temp_size = sum(path.stat().st_size for path in temp_root.rglob("*") if path.is_file())
        assert project_temp_size == max_size_bytes + 2 * len(expected_bytes)
        assert project_temp_size > max_size_bytes

        native_download_after = await client.get(native_file["download_url"])
        assert native_download_after.status_code == 404
        for path in [background_path, *upload_paths]:
            retained_download = await client.get("/api/v1/download", params={"path": str(path)})
            assert retained_download.status_code == 200
            assert retained_download.content == expected_bytes

        deleted_count, current_size = tasks_module._cleanup_temp_dir_by_size(max_size_bytes)
        assert deleted_count == 0
        assert current_size == max_size_bytes
