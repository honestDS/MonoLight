import base64
import io
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

from app.adapters.weixin_openclaw.response import extract_event_reply, extract_reply_files, extract_reply_text
from app.core.constants import (
    ERR_FILE_ARGUMENT_INVALID,
    ERR_FILE_EXTENSION_BLOCKED,
    ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED,
    ERR_FILE_TOTAL_SIZE_LIMIT_EXCEEDED,
    ERR_LLM_IMAGE_OUTPUT_INVALID,
    ERR_LLM_IMAGE_OUTPUT_SAVE_FAILED,
)
from app.core.exceptions import LLMException
from app.core.tools.send_file_to_user import resolve_file_token
from app.core.utils.assistant_files import (
    build_assistant_files_content,
    materialize_generated_images,
    merge_assistant_files,
    parse_assistant_files_content,
)
from app.core.utils.dispatcher.process_markdown_response import process_markdown_response
from app.models.message import InternalGeneratedImage, InternalMessage, MessageRole


def _encoded_image(image_format: str) -> tuple[str, bytes]:
    buffer = io.BytesIO()
    image = Image.new("RGB", (2, 2), color=(12, 34, 56))
    image.save(buffer, format=image_format)
    raw = buffer.getvalue()
    return base64.b64encode(raw).decode("ascii"), raw


def _generated_image(image_id: str, data: str, mime_type: str | None = None) -> InternalGeneratedImage:
    return InternalGeneratedImage(id=image_id, data=data, mime_type=mime_type)


def _cfg(**tool_values):
    return SimpleNamespace(tool=SimpleNamespace(**tool_values))


def _generated_dir(tmp_path, session_id: str):
    return tmp_path / "temp" / f"temp_{session_id}" / "generated_images"


def _patch_token_signing(monkeypatch):
    import app.core.tools.send_file_to_user as send_file_to_user_module

    monkeypatch.setattr(send_file_to_user_module, "_sign_payload", lambda _payload: "test-signature")


def _assert_generated_dir_empty(tmp_path, session_id: str):
    generated_dir = _generated_dir(tmp_path, session_id)
    assert not generated_dir.exists() or not any(generated_dir.iterdir())


def test_assistant_files_content_only_restores_text():
    files = [{"id": "file-1", "name": "generated.png"}]

    content = build_assistant_files_content("图片已发送。", files)
    text = parse_assistant_files_content(content)

    assert json.loads(content)["type"] == "assistant_files"
    assert text == "图片已发送。"


def test_plain_text_content_is_not_treated_as_file_protocol():
    text = parse_assistant_files_content("普通回复")

    assert text == "普通回复"


def test_build_assistant_files_content_unwraps_existing_protocol():
    old_file = {"id": "old-file", "name": "old.png"}
    current_file = {"id": "current-file", "name": "current.png"}
    nested_content = build_assistant_files_content("图片已重新发送。", [old_file])

    content = build_assistant_files_content(nested_content, [current_file])
    payload = json.loads(content)

    assert payload == {
        "type": "assistant_files",
        "text": "图片已重新发送。",
        "files": [current_file],
    }


@pytest.mark.parametrize(
    ("enable_markdown", "files", "expected_text"),
    [
        (False, [{"id": "file-1", "name": "generated.png"}], "图片已发送。\n\n结果一"),
        (True, [{"id": "file-1"}], "**图片**已发送。\n\n- 结果一"),
    ],
)
def test_markdown_processing_inside_assistant_files_protocol(enable_markdown, files, expected_text):
    markdown_text = "**图片**已发送。\n\n- 结果一"
    content = build_assistant_files_content(markdown_text, files)
    message = InternalMessage(role=MessageRole.ASSISTANT, content=content)

    processed = process_markdown_response(message, enable_markdown=enable_markdown)
    payload = json.loads(processed.content)

    if enable_markdown:
        assert processed.content == content

    assert payload["type"] == "assistant_files"
    assert payload["text"] == expected_text
    assert payload["files"] == files


def test_merge_assistant_files_deduplicates_by_id_and_path():
    first = {"id": "file-1", "name": "first.png"}
    duplicate = {"id": "file-1", "name": "duplicate.png"}
    path_only = {"path": "D:/temp/second.png", "name": "second.png"}

    files = merge_assistant_files([first], [duplicate, path_only], [path_only])

    assert files == [first, path_only]


def test_weixin_event_only_trusts_structured_event_files():
    structured_file = {"id": "file-1", "name": "new.png"}
    legacy_file = {"id": "file-2", "name": "legacy.png"}
    event = {
        "content": "图片已发送。",
        "files": [structured_file],
        "history": [
            {
                "role": "assistant",
                "content": build_assistant_files_content("旧格式回复", [structured_file, legacy_file]),
            }
        ],
    }

    text, files = extract_event_reply(event)

    assert text == "图片已发送。"
    assert files == [structured_file]


def test_weixin_event_rejects_files_from_legacy_content():
    legacy_file = {"id": "file-1", "name": "legacy.png"}
    event = {
        "content": build_assistant_files_content("旧格式回复", [legacy_file]),
    }

    text, files = extract_event_reply(event)

    assert text == "旧格式回复"
    assert files == []


def test_weixin_llm_response_only_trusts_top_level_files():
    top_level_file = {"id": "file-1", "name": "new.png"}
    legacy_file = {"id": "file-2", "name": "legacy.png"}
    response = {
        "files": [top_level_file],
        "choices": [
            {
                "message": {
                    "content": build_assistant_files_content("回复文本", [top_level_file, legacy_file]),
                }
            }
        ],
    }

    assert extract_reply_text(response) == "回复文本"
    assert extract_reply_files(response) == [top_level_file]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_format", "mime_type", "extension"),
    [
        ("PNG", "image/png", "png"),
        ("JPEG", "image/jpeg", "jpg"),
        ("WEBP", "image/webp", "webp"),
    ],
)
async def test_materialize_generated_images_writes_supported_formats_and_download_tokens(
    monkeypatch,
    tmp_path,
    image_format,
    mime_type,
    extension,
):
    _patch_token_signing(monkeypatch)
    encoded, raw = _encoded_image(image_format)
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        generated_images=[_generated_image("image-1", encoded)],
    )

    files = await materialize_generated_images(
        message,
        project_root=tmp_path,
        uid="user-1",
        session_id="session-1",
        cfg=_cfg(),
    )

    assert len(files) == 1
    file_item = files[0]
    assert file_item["mime_type"] == mime_type
    assert file_item["name"].endswith(f".{extension}")
    assert not {"path", "data", "base64"}.intersection(file_item)
    assert message.generated_images is None
    assert message.content is None

    generated_dir = _generated_dir(tmp_path, "session-1")
    target = generated_dir / file_item["name"]
    assert target.parent == generated_dir
    assert target.is_file()
    assert target.read_bytes() == raw
    assert not (tmp_path / "temp" / "temp_user-1").exists()

    download_url = urlparse(file_item["download_url"])
    assert download_url.path == "/api/v1/download-sent"
    token = parse_qs(download_url.query)["token"][0]
    assert token == file_item["id"]
    assert resolve_file_token(token) == target.resolve()
    assert resolve_file_token(token).read_bytes() == raw


@pytest.mark.asyncio
async def test_materialize_generated_images_deduplicates_repeated_ids_and_is_stable_across_calls(monkeypatch, tmp_path):
    _patch_token_signing(monkeypatch)
    encoded, raw = _encoded_image("PNG")
    duplicate = _generated_image("same-id", encoded, "image/png")
    first_message = InternalMessage(role=MessageRole.ASSISTANT, generated_images=[duplicate, duplicate])

    first = await materialize_generated_images(
        first_message,
        project_root=tmp_path,
        uid="user-1",
        session_id="session-1",
        cfg=_cfg(),
    )

    second_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        generated_images=[_generated_image("same-id", encoded, "image/png")],
    )
    second = await materialize_generated_images(
        second_message,
        project_root=tmp_path,
        uid="user-1",
        session_id="session-1",
        cfg=_cfg(),
    )

    assert len(first) == 1
    assert second == first
    assert first_message.generated_images is None
    assert second_message.generated_images is None
    generated_dir = _generated_dir(tmp_path, "session-1")
    assert [path.name for path in generated_dir.iterdir()] == [first[0]["name"]]
    assert (generated_dir / first[0]["name"]).read_bytes() == raw


@pytest.mark.asyncio
async def test_materialize_generated_images_keeps_same_bytes_for_different_ids(monkeypatch, tmp_path):
    _patch_token_signing(monkeypatch)
    encoded, raw = _encoded_image("WEBP")
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        generated_images=[
            _generated_image("image-a", encoded, "image/webp"),
            _generated_image("image-b", encoded, "image/webp"),
        ],
    )

    files = await materialize_generated_images(
        message,
        project_root=tmp_path,
        uid="user-1",
        session_id="session-1",
        cfg=_cfg(),
    )

    assert len(files) == 2
    assert len({item["id"] for item in files}) == 2
    assert len({item["name"] for item in files}) == 2
    generated_dir = _generated_dir(tmp_path, "session-1")
    assert all((generated_dir / item["name"]).read_bytes() == raw for item in files)


@pytest.mark.asyncio
async def test_materialize_generated_images_isolates_sessions(monkeypatch, tmp_path):
    _patch_token_signing(monkeypatch)
    encoded, raw = _encoded_image("PNG")

    first_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        generated_images=[_generated_image("image-1", encoded, "image/png")],
    )
    first = await materialize_generated_images(
        first_message,
        project_root=tmp_path,
        uid="user-1",
        session_id="session-a",
        cfg=_cfg(),
    )
    second_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        generated_images=[_generated_image("image-1", encoded, "image/png")],
    )
    second = await materialize_generated_images(
        second_message,
        project_root=tmp_path,
        uid="user-1",
        session_id="session-b",
        cfg=_cfg(),
    )

    first_path = _generated_dir(tmp_path, "session-a") / first[0]["name"]
    second_path = _generated_dir(tmp_path, "session-b") / second[0]["name"]
    assert first_path != second_path
    assert first_path.is_file() and second_path.is_file()
    assert first_path.read_bytes() == raw
    assert second_path.read_bytes() == raw
    assert resolve_file_token(first[0]["id"]) == first_path.resolve()
    assert resolve_file_token(second[0]["id"]) == second_path.resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["base64", "non_image", "mime_mismatch", "gif"])
async def test_materialize_generated_images_rejects_invalid_image_output(monkeypatch, tmp_path, invalid_kind):
    _patch_token_signing(monkeypatch)
    if invalid_kind == "base64":
        data = "not-valid-base64"
        mime_type = None
    elif invalid_kind == "non_image":
        data = base64.b64encode(b"not an image").decode("ascii")
        mime_type = None
    elif invalid_kind == "mime_mismatch":
        data, _raw = _encoded_image("PNG")
        mime_type = "image/jpeg"
    else:
        data, _raw = _encoded_image("GIF")
        mime_type = "image/gif"

    image = _generated_image("invalid-image", data, mime_type)
    message = InternalMessage(role=MessageRole.ASSISTANT, generated_images=[image])

    with pytest.raises(LLMException) as exc_info:
        await materialize_generated_images(
            message,
            project_root=tmp_path,
            uid="user-1",
            session_id="session-1",
            cfg=_cfg(),
        )

    assert exc_info.value.message == ERR_LLM_IMAGE_OUTPUT_INVALID
    assert message.generated_images == [image]
    _assert_generated_dir_empty(tmp_path, "session-1")


@pytest.mark.asyncio
async def test_materialize_generated_images_rejects_single_file_limit_without_large_fixture(monkeypatch, tmp_path):
    _patch_token_signing(monkeypatch)
    encoded, raw = _encoded_image("PNG")
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        generated_images=[_generated_image("large-image", encoded, "image/png")],
    )
    max_single_size_mb = (len(raw) - 1) / (1024 * 1024)

    with pytest.raises(LLMException) as exc_info:
        await materialize_generated_images(
            message,
            project_root=tmp_path,
            uid="user-1",
            session_id="session-1",
            cfg=_cfg(file_send_max_single_size_mb=max_single_size_mb),
        )

    assert exc_info.value.message == ERR_FILE_SINGLE_SIZE_LIMIT_EXCEEDED
    assert message.generated_images is not None
    _assert_generated_dir_empty(tmp_path, "session-1")


@pytest.mark.asyncio
async def test_materialize_generated_images_rejects_total_size_limit_without_large_fixture(monkeypatch, tmp_path):
    _patch_token_signing(monkeypatch)
    encoded, raw = _encoded_image("PNG")
    images = [
        _generated_image("image-a", encoded, "image/png"),
        _generated_image("image-b", encoded, "image/png"),
    ]
    message = InternalMessage(role=MessageRole.ASSISTANT, generated_images=images)
    max_total_size_mb = (len(raw) * 2 - 1) / (1024 * 1024)

    with pytest.raises(LLMException) as exc_info:
        await materialize_generated_images(
            message,
            project_root=tmp_path,
            uid="user-1",
            session_id="session-1",
            cfg=_cfg(file_send_max_total_size_mb=max_total_size_mb),
        )

    assert exc_info.value.message == ERR_FILE_TOTAL_SIZE_LIMIT_EXCEEDED
    assert message.generated_images == images
    _assert_generated_dir_empty(tmp_path, "session-1")


@pytest.mark.asyncio
async def test_materialize_generated_images_rejects_file_count_limit(monkeypatch, tmp_path):
    _patch_token_signing(monkeypatch)
    encoded, _raw = _encoded_image("PNG")
    images = [
        _generated_image("image-a", encoded, "image/png"),
        _generated_image("image-b", encoded, "image/png"),
    ]
    message = InternalMessage(role=MessageRole.ASSISTANT, generated_images=images)

    with pytest.raises(LLMException) as exc_info:
        await materialize_generated_images(
            message,
            project_root=tmp_path,
            uid="user-1",
            session_id="session-1",
            cfg=_cfg(file_send_max_count=1),
        )

    assert exc_info.value.message == ERR_FILE_ARGUMENT_INVALID
    assert message.generated_images == images
    _assert_generated_dir_empty(tmp_path, "session-1")


@pytest.mark.asyncio
async def test_materialize_generated_images_blocks_jpeg_when_jpeg_extension_is_blocked(monkeypatch, tmp_path):
    _patch_token_signing(monkeypatch)
    encoded, _raw = _encoded_image("JPEG")
    image = _generated_image("jpeg-image", encoded, "image/jpeg")
    message = InternalMessage(role=MessageRole.ASSISTANT, generated_images=[image])

    with pytest.raises(LLMException) as exc_info:
        await materialize_generated_images(
            message,
            project_root=tmp_path,
            uid="user-1",
            session_id="session-1",
            cfg=_cfg(file_send_blocked_extensions=[".jpeg"]),
        )

    assert exc_info.value.message == ERR_FILE_EXTENSION_BLOCKED
    assert message.generated_images == [image]
    _assert_generated_dir_empty(tmp_path, "session-1")


@pytest.mark.asyncio
async def test_materialize_generated_images_rejects_session_traversal(monkeypatch, tmp_path):
    _patch_token_signing(monkeypatch)
    encoded, _raw = _encoded_image("PNG")
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        generated_images=[_generated_image("image-1", encoded, "image/png")],
    )

    with pytest.raises(LLMException) as exc_info:
        await materialize_generated_images(
            message,
            project_root=tmp_path,
            uid="user-1",
            session_id="../escape",
            cfg=_cfg(),
        )

    assert exc_info.value.message == ERR_FILE_ARGUMENT_INVALID
    assert message.generated_images is not None
    assert not (tmp_path / "escape").exists()


@pytest.mark.asyncio
async def test_materialize_generated_images_does_not_keep_valid_files_when_batch_contains_invalid_image(monkeypatch, tmp_path):
    _patch_token_signing(monkeypatch)
    encoded, _raw = _encoded_image("PNG")
    images = [
        _generated_image("valid-image", encoded, "image/png"),
        _generated_image("invalid-image", "not-valid-base64", None),
    ]
    message = InternalMessage(role=MessageRole.ASSISTANT, generated_images=images)

    with pytest.raises(LLMException) as exc_info:
        await materialize_generated_images(
            message,
            project_root=tmp_path,
            uid="user-1",
            session_id="session-1",
            cfg=_cfg(),
        )

    assert exc_info.value.message == ERR_LLM_IMAGE_OUTPUT_INVALID
    assert message.generated_images == images
    _assert_generated_dir_empty(tmp_path, "session-1")


@pytest.mark.asyncio
async def test_materialize_generated_images_cleans_temporary_file_when_storage_fails(monkeypatch, tmp_path):
    _patch_token_signing(monkeypatch)
    encoded, _raw = _encoded_image("PNG")
    image = _generated_image("image-1", encoded, "image/png")
    message = InternalMessage(role=MessageRole.ASSISTANT, generated_images=[image])

    import app.core.utils.assistant_files as assistant_files_module

    def fail_replace(_source, _target):
        raise OSError("storage unavailable")

    monkeypatch.setattr(assistant_files_module.os, "replace", fail_replace)

    with pytest.raises(LLMException) as exc_info:
        await materialize_generated_images(
            message,
            project_root=tmp_path,
            uid="user-1",
            session_id="session-1",
            cfg=_cfg(),
        )

    assert exc_info.value.message == ERR_LLM_IMAGE_OUTPUT_SAVE_FAILED
    assert message.generated_images == [image]
    generated_dir = _generated_dir(tmp_path, "session-1")
    assert generated_dir.exists()
    assert not list(generated_dir.glob("*.tmp"))
    assert not list(generated_dir.glob(".*.tmp"))
    assert not list(generated_dir.glob("generated_image_*"))
