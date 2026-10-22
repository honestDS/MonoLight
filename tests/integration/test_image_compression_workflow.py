import base64
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select

import app.core.crud.channel.cursor as channel_cursor_module
import app.core.dispatchers.dispatcher as dispatcher_module
from app.core.constants import READ_MULTIMODAL_FILE_TOOL_NAME
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, Message, MessageRole
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.user import User
from app.providers.llm.client import LLMClient
from app.transformers.openai import (
    OpenAIChatCompletionsTransformer,
    OpenAIResponsesTransformer,
)

UID = "image-compression-user"
CHANNEL_ID = 1
PROFILE_ID = 1
MODEL_ID = "test-vision-model"
IMAGE_SIZE = (32, 24)
FINAL_REPLY = "The image was read successfully."


def _provider_payload(protocol: str, generation_kwargs: dict[str, Any]) -> Any:
    transformer_type = {
        "OPENAI": OpenAIChatCompletionsTransformer,
        "OPENAI_RESPONSES": OpenAIResponsesTransformer,
    }[protocol]
    return transformer_type.to_provider(
        generation_kwargs["messages"],
        channel_id=generation_kwargs.get("channel_id"),
        model_id=generation_kwargs["model_id"],
    )


def _image_data_urls(messages: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if part.get("type") == "image_url":
                found.append(part["image_url"]["url"])
            elif part.get("type") == "input_image":
                found.append(part["image_url"])
    return found


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"], ids=["chat-completions", "responses"])
@pytest.mark.parametrize("entrypoint", ["attachment", "tool"], ids=["attachment", "read-tool"])
@pytest.mark.parametrize(
    ("compression_enabled", "threshold_kb", "expected_format", "pad_to_size"),
    [
        pytest.param(None, None, "PNG", None, id="defaults-500kb"),
        pytest.param(False, 1, "PNG", None, id="disabled"),
        pytest.param(True, 1, "JPEG", None, id="compress-over-1kb"),
        pytest.param(True, 64, "PNG", None, id="below-64kb"),
        pytest.param(True, 4, "PNG", 4096, id="exactly-4kb"),
        pytest.param(True, 4, "JPEG", 4097, id="over-4kb"),
    ],
)
async def test_image_compression_across_real_dialogue_entrypoints(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    protocol: str,
    entrypoint: str,
    compression_enabled: bool | None,
    threshold_kb: int | None,
    expected_format: str,
    pad_to_size: int | None,
) -> None:
    image_path = (tmp_path / "source.png").resolve()
    pixels = bytes((index * 37) % 256 for index in range(IMAGE_SIZE[0] * IMAGE_SIZE[1] * 4))
    Image.frombytes("RGBA", IMAGE_SIZE, pixels).save(image_path, format="PNG", compress_level=0)
    source_bytes = image_path.read_bytes()
    assert 1024 < len(source_bytes) < 4096
    if pad_to_size is not None:
        image_path.write_bytes(source_bytes + bytes(pad_to_size - len(source_bytes)))
    disk_bytes_before = image_path.read_bytes()
    assert len(disk_bytes_before) == (pad_to_size or len(source_bytes))
    source_path = str(image_path)

    tool_config: dict[str, Any] = {
        "enabled_tools": [READ_MULTIMODAL_FILE_TOOL_NAME],
        "allowed_operation_dirs": [str(tmp_path.resolve())],
    }
    if compression_enabled is not None:
        tool_config["image_compression_enabled"] = compression_enabled
    if threshold_kb is not None:
        tool_config["image_compression_threshold_kb"] = threshold_kb
    profile_configs = {
        "channel": {"chat_channel": {"rules": [{"channel_id": CHANNEL_ID, "model_id": MODEL_ID, "priority": 1, "weight": 100}]}},
        "security": {"audit_threshold": 0},
        "tool": tool_config,
        "memory": {"enabled": False},
    }
    session_id = f"image-compression-{entrypoint}-{protocol.lower()}-{expected_format}-{pad_to_size or 'base'}"
    records: list[dict[str, Any]] = []

    async with database_factory(foreign_keys=True) as factory:
        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
        monkeypatch.setattr(channel_cursor_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(dispatcher_module, "AsyncSessionLocal", factory)

        async with factory() as db:
            db.add(User(uid=UID, username="image_compression_user"))
            db.add(
                ModelChannel(
                    id=CHANNEL_ID,
                    name="image-compression-channel",
                    base_url="https://example.invalid/v1",
                    api_key="test-encrypted-key",
                    is_active=True,
                    model_ids=[
                        {
                            "model_id": MODEL_ID,
                            "usage": "CHAT",
                            "protocol": protocol,
                            "image_understanding": True,
                            "context_window_k": 64,
                            "max_tokens": 512,
                        }
                    ],
                )
            )
            await db.flush()
            db.add(Profile(id=PROFILE_ID, uid=UID, name="image-compression-profile", configs=profile_configs, is_default=True))
            await db.flush()
            db.add(
                ChatSession(
                    session_id=session_id,
                    uid=UID,
                    profile_id=PROFILE_ID,
                    goal_mode=False,
                    max_turns=5,
                    stream_llm_request=False,
                    source="http",
                )
            )
            await db.commit()

        async def fake_generate(**generation_kwargs: Any) -> InternalResponse:
            records.append(
                {
                    "messages": generation_kwargs["messages"],
                    "payload": _provider_payload(protocol, generation_kwargs),
                }
            )
            if entrypoint == "tool" and len(records) == 1:
                message = InternalMessage(
                    role=MessageRole.ASSISTANT,
                    content="",
                    tool_calls=[
                        InternalToolCall(
                            id="read-image-call",
                            name=READ_MULTIMODAL_FILE_TOOL_NAME,
                            arguments={"path": source_path},
                        )
                    ],
                )
            else:
                message = InternalMessage(role=MessageRole.ASSISTANT, content=FINAL_REPLY)
            return InternalResponse(message=message, model=MODEL_ID)

        monkeypatch.setattr(LLMClient, "generate", staticmethod(fake_generate))
        attachments = [source_path] if entrypoint == "attachment" else None
        original_attachments = list(attachments) if attachments is not None else None
        async with factory() as db:
            await ChatDispatcher.dispatch(
                db,
                message="Please inspect this image.",
                uid=UID,
                session_id=session_id,
                attachments=attachments,
                session_source="http",
            )

        async with factory() as db:
            persisted_messages = (await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all()
        assert any(message.role == MessageRole.ASSISTANT and message.content == FINAL_REPLY for message in persisted_messages)
        persisted_user_messages = [message for message in persisted_messages if message.role == MessageRole.USER]
        assert persisted_user_messages[-1].attachments == original_attachments
        assert image_path.read_bytes() == disk_bytes_before
        assert attachments == original_attachments

    assert len(records) == (2 if entrypoint == "tool" else 1)
    request_urls = [_image_data_urls(record["payload"]) for record in records]
    if entrypoint == "tool":
        assert request_urls[0] == []
        assert len(request_urls[1]) == 1
        tool_result = next(message for message in records[1]["messages"] if message.role == MessageRole.TOOL and message.tool_call_id == "read-image-call")
        tool_payload = json.loads(tool_result.content)
        assert tool_payload["type"] == "multimodal_file_read"
        assert tool_payload["status"] == "success"
        assert tool_payload["path"] == source_path
    else:
        assert len(request_urls[0]) == 1

    data_url = request_urls[-1][0]
    header, encoded = data_url.split(",", 1)
    mime_type = header.removeprefix("data:").removesuffix(";base64")
    image_bytes = base64.b64decode(encoded)
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.load()
        actual_format = image.format
        actual_size = image.size
        actual_mode = image.mode
    assert mime_type == {"PNG": "image/png", "JPEG": "image/jpeg"}[actual_format]
    assert actual_format == expected_format
    if expected_format == "PNG":
        assert image_bytes == disk_bytes_before
        assert actual_mode == "RGBA"
        assert actual_size == IMAGE_SIZE
    else:
        assert actual_size[0] >= 1 and actual_size[1] >= 1
        assert actual_size[0] < IMAGE_SIZE[0] and actual_size[1] < IMAGE_SIZE[1]
