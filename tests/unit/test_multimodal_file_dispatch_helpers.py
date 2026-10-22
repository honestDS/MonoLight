import base64
import io
import json
from urllib.parse import quote_from_bytes, unquote_to_bytes

import pytest
from PIL import Image, PngImagePlugin

from app.core.constants import (
    ERR_LLM_IMAGE_INPUT_INVALID,
    ERR_TOOL_MULTIMODAL_FILE_NOT_FOUND,
    MSG_TOOL_MULTIMODAL_FILE_READ_SUCCESS,
)
from app.core.dispatchers.interactive_helpers import (
    build_pending_multimodal_input_message,
    collect_pending_multimodal_file_inputs,
)
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.tools.read_multimodal_file import ReadMultimodalFileExecutor
from app.core.utils.message_assembler import MessageAssembler
from app.models.message import (
    AudioPart,
    FilePart,
    ImagePart,
    InternalMessage,
    InternalToolCall,
    MessageRole,
    TextPart,
    VoiceTextPart,
)
from app.models.profile import ProfileConfig, ToolConfig
from tests.unit.response_test_support import multimodal_read_payload


def _success_result(path, message="不是用户的新输入", modality="image"):
    return json.dumps(
        multimodal_read_payload(path, message=message, modality=modality),
        ensure_ascii=False,
    )


@pytest.mark.parametrize("modality", ["image", "audio", "missing"])
@pytest.mark.asyncio
async def test_read_multimodal_file_executor_response_contract(tmp_path, modality):
    suffix = {"image": ".png", "audio": ".mp3", "missing": ".png"}[modality]
    path = tmp_path / f"{modality}{suffix}"
    if modality == "image":
        Image.new("RGB", (2, 2), color=(10, 20, 30)).save(path, format="PNG")
    elif modality == "audio":
        path.write_bytes(b"audio-bytes")

    executor = ReadMultimodalFileExecutor(project_root=str(tmp_path), uid="user-1")
    profile = ProfileConfig.model_validate({"tool": {"allowed_operation_dirs": [str(tmp_path)]}})
    executor.set_config(profile)
    actual = json.loads(await executor.execute(path=str(path)))

    if modality == "missing":
        error = t(ERR_TOOL_MULTIMODAL_FILE_NOT_FOUND)
        expected = {
            "type": "multimodal_file_read",
            "status": "failed",
            "error": error,
            "path": str(path.resolve()),
        }
        assert actual == expected
        assert multimodal_read_payload(path, error=error) == expected
        return

    message = t(MSG_TOOL_MULTIMODAL_FILE_READ_SUCCESS)
    expected = {
        "type": "multimodal_file_read",
        "status": "success",
        "modality": modality,
        "path": str(path.resolve()),
        "message": message,
    }
    assert actual == expected
    assert multimodal_read_payload(path, message=message, modality=modality) == expected


@pytest.fixture
def inline_png_bytes():
    buffer = io.BytesIO()
    Image.new("RGBA", (32, 24), color=(20, 80, 140, 180)).save(
        buffer,
        format="PNG",
        compress_level=0,
    )
    return buffer.getvalue()


def _png_with_exact_size(image_bytes, size):
    padding_length = size - len(image_bytes) - 20
    assert padding_length > 0

    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("padding", "x" * padding_length)
    buffer = io.BytesIO()
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.save(buffer, format="PNG", compress_level=0, pnginfo=png_info)

    result = buffer.getvalue()
    assert len(result) == size
    return result


def _inline_data_url(image_bytes, encoding, header="data:image/png"):
    if encoding == "base64":
        return f"{header};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    return f"{header},{quote_from_bytes(image_bytes, safe='')}"


def _decode_inline_data_url(url):
    header, separator, encoded_data = url.partition(",")
    assert separator
    if any(parameter.strip().lower() == "base64" for parameter in header.split(";")[1:]):
        return base64.b64decode(encoded_data)
    return unquote_to_bytes(encoded_data)


def _image_format_and_size(image_bytes):
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.load()
        return image.format, image.size


def test_collect_pending_multimodal_file_inputs_uses_tool_call_order_and_skips_failures(tmp_path):
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    first_call = InternalToolCall(id="call-first", name="read_multimodal_file", arguments={"path": str(first_path)})
    second_call = InternalToolCall(id="call-second", name="read_multimodal_file", arguments={"path": str(second_path)})
    failed_call = InternalToolCall(id="call-failed", name="read_multimodal_file", arguments={"path": str(tmp_path / "failed.png")})

    messages = [
        InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[first_call, second_call, failed_call],
        ),
        InternalMessage(role=MessageRole.TOOL, tool_call_id=second_call.id, content=_success_result(second_path)),
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=failed_call.id,
            content=json.dumps(
                multimodal_read_payload(tmp_path / "failed.png", error="failed"),
                ensure_ascii=False,
            ),
        ),
        InternalMessage(role=MessageRole.TOOL, tool_call_id=first_call.id, content=_success_result(first_path)),
    ]

    collected = collect_pending_multimodal_file_inputs(messages)

    assert [item["tool_call_id"] for item in collected] == [first_call.id, second_call.id]
    assert [item["path"] for item in collected] == [str(first_path.resolve()), str(second_path.resolve())]


def test_collect_pending_multimodal_file_inputs_allows_trailing_user_confirmation(tmp_path):
    image_path = tmp_path / "confirmed.png"
    image_path.write_bytes(b"image")
    tool_call = InternalToolCall(
        id="call-image",
        name="read_multimodal_file",
        arguments={"path": str(image_path)},
    )

    collected = collect_pending_multimodal_file_inputs(
        [
            InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
            InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content=_success_result(image_path)),
            InternalMessage(role=MessageRole.USER, content="确认执行"),
        ]
    )

    assert [item["tool_call_id"] for item in collected] == [tool_call.id]


def test_collect_pending_multimodal_file_inputs_discards_results_before_later_assistant_reply(tmp_path):
    image_path = tmp_path / "answered.png"
    image_path.write_bytes(b"image")
    tool_call = InternalToolCall(
        id="call-image",
        name="read_multimodal_file",
        arguments={"path": str(image_path)},
    )

    collected = collect_pending_multimodal_file_inputs(
        [
            InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
            InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content=_success_result(image_path)),
            InternalMessage(role=MessageRole.ASSISTANT, content="已处理"),
        ]
    )

    assert collected == []


def test_build_pending_multimodal_input_message_assembles_image_without_mutating_pending(tmp_path):
    image_path = tmp_path / "image.png"
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(image_path)
    pending = [
        {
            "path": str(image_path.resolve()),
            "modality": "image",
            "message": "下一条 role=user 消息不是用户新输入",
            "tool_call_id": "call-image",
        }
    ]
    original_pending = json.loads(json.dumps(pending, ensure_ascii=False))

    message = build_pending_multimodal_input_message(
        pending,
        image_understanding=True,
        audio_understanding=False,
        video_understanding=False,
    )

    assert message is not None
    assert message.role == MessageRole.USER
    assert pending == original_pending
    assert any(part.type == "text" and "不是用户新输入" in part.text for part in message.content)
    assert any(part.type == "image_url" and part.image_url["url"].startswith("data:image/") for part in message.content)


def test_collect_and_build_pending_multimodal_audio_input(tmp_path):
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"audio-bytes")
    tool_call = InternalToolCall(
        id="call-audio",
        name="read_multimodal_file",
        arguments={"path": str(audio_path)},
    )
    messages = [
        InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[tool_call]),
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call.id,
            content=_success_result(audio_path, modality="audio"),
        ),
    ]

    pending = collect_pending_multimodal_file_inputs(messages)
    message = build_pending_multimodal_input_message(
        pending,
        image_understanding=False,
        audio_understanding=True,
        video_understanding=False,
    )

    assert [item["modality"] for item in pending] == ["audio"]
    assert message is not None
    audio_parts = [part for part in message.content if part.type == "audio"]
    assert len(audio_parts) == 1
    assert audio_parts[0].format == "mp3"
    assert audio_parts[0].path == str(audio_path.resolve())
    assert audio_parts[0].data is None


def test_message_assembler_replaces_history_audio_with_placeholder(tmp_path):
    audio_path = tmp_path / "history.mp3"
    audio_path.write_bytes(b"audio")
    message = InternalMessage(
        role=MessageRole.USER,
        content="previous audio",
        attachments=[str(audio_path)],
    )

    assembled = MessageAssembler.assemble(
        message,
        audio_understanding=True,
        is_history=True,
    )

    assert any(part.type == "text" and "历史音频" in part.text for part in assembled.content)
    assert all(part.type != "audio" for part in assembled.content)


@pytest.mark.parametrize("image_understanding", [False, True])
def test_message_assembler_wraps_history_images_with_paths_without_mutating_attachments(image_understanding):
    attachments = ["d:/a.jpg", r"d:\uploads\历史 图片.PNG"]
    message = InternalMessage(
        role=MessageRole.USER,
        content="正文保留",
        attachments=attachments,
    )

    assembled = MessageAssembler.assemble(
        message,
        image_understanding=image_understanding,
        audio_understanding=False,
        video_understanding=False,
        is_history=True,
    )

    assert assembled.attachments == attachments
    assert [(part.type, part.text) for part in assembled.content] == [
        ("text", "正文保留"),
        (
            "text",
            "[系统提示,此处不是用户说的话]"
            "[历史图片：d:/a.jpg]"
            r"[历史图片：d:\uploads\历史 图片.PNG]"
            "[系统提示结束]",
        ),
    ]
    assert all(part.type != "image_url" for part in assembled.content)


def test_message_assembler_keeps_history_image_wrapper_stable_after_reassembly_and_restore():
    attachments = ["history-first.jpg", "history-second.png"]
    message = InternalMessage(
        role=MessageRole.USER,
        content="历史正文",
        attachments=attachments,
    )
    expected_content = [
        ("text", "历史正文"),
        (
            "text",
            "[系统提示,此处不是用户说的话][历史图片：history-first.jpg][历史图片：history-second.png][系统提示结束]",
        ),
    ]

    for _ in range(2):
        assembled = MessageAssembler.assemble(message, is_history=True)
        assert [(part.type, part.text) for part in assembled.content] == expected_content
        assert assembled.attachments == attachments

    restored = InternalMessage.model_validate(message.model_dump(mode="json"))
    assembled = MessageAssembler.assemble(restored, is_history=True)

    assert [(part.type, part.text) for part in assembled.content] == expected_content
    assert assembled.attachments == attachments


def test_message_assembler_keeps_mixed_attachment_contract_stable_across_capability_switches(tmp_path):
    first_image = tmp_path / "first.png"
    second_image = tmp_path / "second.png"
    Image.new("RGB", (2, 2), color=(10, 20, 30)).save(first_image)
    Image.new("RGB", (2, 2), color=(30, 20, 10)).save(second_image)
    audio_path = tmp_path / "sample.mp3"
    video_path = tmp_path / "clip.mp4"
    pdf_path = tmp_path / "document.pdf"
    attachments = [
        str(first_image),
        str(second_image),
        str(audio_path),
        str(video_path),
        str(pdf_path),
    ]
    message = InternalMessage(
        role=MessageRole.USER,
        content="混合附件正文",
        attachments=attachments,
    )

    MessageAssembler.assemble(
        message,
        image_understanding=False,
        audio_understanding=False,
        video_understanding=False,
    )
    false_content_snapshot = message.model_dump(mode="json")["content"]
    false_attachments_snapshot = list(message.attachments or [])

    assembled = MessageAssembler.assemble(
        message,
        image_understanding=True,
        audio_understanding=True,
        video_understanding=True,
    )
    assert assembled.content[0].type == "text"
    assert assembled.content[0].text == "混合附件正文"
    assert assembled.attachments == attachments
    assert sum(part.type == "text" and part.text == "[系统提示,此处不是用户说的话]" for part in assembled.content) == 1
    assert sum(part.type == "text" and part.text == "[系统提示结束]" for part in assembled.content) == 1
    image_parts = [part for part in assembled.content if part.type == "image_url"]
    assert len(image_parts) == 2
    assert all(part.image_url["url"].startswith("data:image/png;base64,") for part in image_parts)
    audio_parts = [part for part in assembled.content if part.type == "audio"]
    assert len(audio_parts) == 1
    assert audio_parts[0].path == str(audio_path)
    assert audio_parts[0].format == "mp3"
    file_parts = [part for part in assembled.content if part.type == "file"]
    assert [part.path for part in file_parts] == [str(video_path), str(pdf_path)]

    restored = InternalMessage.model_validate(message.model_dump(mode="json"))
    for _ in range(2):
        MessageAssembler.assemble(
            restored,
            image_understanding=False,
            audio_understanding=False,
            video_understanding=False,
        )
        assert restored.model_dump(mode="json")["content"] == false_content_snapshot
        assert restored.attachments == false_attachments_snapshot


@pytest.mark.parametrize(
    "attachments",
    [
        [],
        ["virtual/single.txt"],
        [
            "virtual/first.jpg",
            "virtual/second.mp3",
            "virtual/third.mp4",
            "virtual/fourth.pdf",
            "virtual/fifth.bin",
            "virtual/sixth.txt",
        ],
    ],
)
def test_message_assembler_preserves_mixed_content_parts_across_reassembly(attachments):
    original_parts = [
        VoiceTextPart(text="语音正文", input_source="voice"),
        TextPart(text="普通正文"),
        ImagePart(image_url={"url": "data:image/png;base64,original"}),
        AudioPart(format="mp3", path="virtual/original.mp3"),
        FilePart(path="virtual/original.pdf"),
    ]
    message = InternalMessage.from_user_input(
        content=original_parts,
        attachments=attachments,
    )
    original_message_dump = message.model_dump(mode="json")
    expected_prefix = original_message_dump["content"]

    assembled = MessageAssembler.assemble(
        message,
        image_understanding=False,
        audio_understanding=False,
        video_understanding=False,
    )
    first_content = assembled.model_dump(mode="json")["content"]
    assert first_content[: len(expected_prefix)] == expected_prefix

    for _ in range(2):
        assembled = MessageAssembler.assemble(
            message,
            image_understanding=False,
            audio_understanding=False,
            video_understanding=False,
        )
        assert assembled.model_dump(mode="json")["content"] == first_content

    restored = InternalMessage.model_validate(message.model_dump(mode="json"))
    assert restored.model_dump(mode="json")["content"] == first_content
    restored = MessageAssembler.assemble(
        restored,
        image_understanding=False,
        audio_understanding=False,
        video_understanding=False,
    )
    assert restored.model_dump(mode="json")["content"] == first_content

    switched = MessageAssembler.assemble(
        restored,
        image_understanding=False,
        audio_understanding=True,
        video_understanding=True,
    )
    switched_content = switched.model_dump(mode="json")["content"]
    assert switched_content[: len(expected_prefix)] == expected_prefix

    expected_switched = MessageAssembler.assemble(
        InternalMessage.model_validate(original_message_dump),
        image_understanding=False,
        audio_understanding=True,
        video_understanding=True,
    )
    assert switched_content == expected_switched.model_dump(mode="json")["content"]


def test_message_assembler_replaces_legacy_checkpoint_attachment_parts_without_explicit_count():
    attachments = ["checkpoint-first.jpg", "checkpoint-second.png"]
    message = InternalMessage(
        role=MessageRole.USER,
        content=[
            TextPart(text="第一正文"),
            TextPart(text="第二正文"),
            TextPart(
                text="[系统提示,此处不是用户说的话][历史图片：checkpoint-first.jpg][系统提示结束]",
            ),
            TextPart(
                text="[系统提示,此处不是用户说的话][历史图片：checkpoint-second.png][系统提示结束]",
            ),
        ],
        attachments=attachments,
    )
    expected_content = [
        ("text", "第一正文"),
        ("text", "第二正文"),
        (
            "text",
            "[系统提示,此处不是用户说的话][历史图片：checkpoint-first.jpg][历史图片：checkpoint-second.png][系统提示结束]",
        ),
    ]

    for _ in range(2):
        assembled = MessageAssembler.assemble(message, is_history=True)
        assert [(part.type, part.text) for part in assembled.content] == expected_content
        assert assembled.attachments == attachments


def test_message_assembler_wraps_disabled_mixed_attachments_in_content_order():
    attachments = ["photo.jpg", "sound.mp3", "clip.mp4", "document.pdf"]
    message = InternalMessage(
        role=MessageRole.USER,
        content="正文原样",
        attachments=attachments,
    )

    assembled = MessageAssembler.assemble(
        message,
        image_understanding=False,
        audio_understanding=False,
        video_understanding=False,
    )

    assert [(part.type, part.text if part.type == "text" else part.path) for part in assembled.content] == [
        ("text", "正文原样"),
        ("text", "[系统提示,此处不是用户说的话][未开启图像理解无法解析图片: photo.jpg][未开启音频理解: sound.mp3][未开启视频理解: clip.mp4]"),
        ("file", "document.pdf"),
        ("text", "[系统提示结束]"),
    ]


@pytest.mark.parametrize(
    "content",
    [
        "无附件字符串正文",
        [TextPart(text="无附件文本片段")],
    ],
)
def test_message_assembler_preserves_content_without_attachments(content):
    message = InternalMessage(
        role=MessageRole.USER,
        content=content,
        attachments=[],
    )

    assembled = MessageAssembler.assemble(message)

    assert assembled.content == content


@pytest.mark.parametrize(
    "corrupt, expected_text",
    [
        (False, "[图片丢失: "),
        (True, "[图片处理失败: "),
    ],
)
def test_message_assembler_wraps_image_processing_results(corrupt, expected_text, tmp_path):
    image_path = tmp_path / "input.png"
    if corrupt:
        image_path.write_bytes(b"not-an-image")

    message = InternalMessage(
        role=MessageRole.USER,
        content="图片正文",
        attachments=[str(image_path)],
    )

    assembled = MessageAssembler.assemble(
        message,
        image_understanding=True,
        audio_understanding=False,
        video_understanding=False,
    )

    image_result = f"{expected_text}{image_path}]"
    assert [(part.type, part.text) for part in assembled.content] == [
        ("text", "图片正文"),
        ("text", f"[系统提示,此处不是用户说的话]{image_result}[系统提示结束]"),
    ]


@pytest.mark.parametrize(
    ("encoding", "header"),
    [
        ("base64", "DATA:IMAGE/PNG"),
        ("percent", "dAtA:ImAgE/PnG"),
    ],
)
@pytest.mark.parametrize(
    ("enabled", "threshold_kb", "target_size", "expected"),
    [
        (False, 1, None, "original"),
        (True, 4, None, "original"),
        (True, 4, 4096, "original"),
        (True, 4, 4097, "jpeg"),
        (True, 1, None, "jpeg"),
    ],
    ids=["disabled", "below-threshold", "at-threshold", "above-threshold", "one-kb-threshold"],
)
def test_prepare_inline_images_decodes_data_urls_and_compresses_only_above_threshold(
    inline_png_bytes,
    encoding,
    header,
    enabled,
    threshold_kb,
    target_size,
    expected,
):
    image_bytes = inline_png_bytes if target_size is None else _png_with_exact_size(inline_png_bytes, target_size)
    if expected == "jpeg":
        assert len(image_bytes) > threshold_kb * 1024
    elif enabled:
        assert len(image_bytes) <= threshold_kb * 1024

    source_url = _inline_data_url(image_bytes, encoding, header=header)
    source_part = ImagePart(image_url={"url": source_url, "detail": "high"})
    message = InternalMessage(role=MessageRole.USER, content=[source_part])
    source_message_snapshot = message.model_dump(mode="json")
    source_part_snapshot = source_part.model_dump(mode="json")
    tool_config = ToolConfig(
        image_compression_enabled=enabled,
        image_compression_threshold_kb=threshold_kb,
    )

    first_result = MessageAssembler.prepare_inline_images(message, tool_config)
    second_result = MessageAssembler.prepare_inline_images(message, tool_config)

    assert first_result.model_dump(mode="json") == second_result.model_dump(mode="json")
    assert message.model_dump(mode="json") == source_message_snapshot
    assert source_part.model_dump(mode="json") == source_part_snapshot
    output_part = first_result.content[0]
    assert output_part.image_url["detail"] == "high"

    output_url = output_part.image_url["url"]
    output_bytes = _decode_inline_data_url(output_url)
    if expected == "original":
        assert output_url == source_url
        assert output_bytes == image_bytes
        assert _image_format_and_size(output_bytes) == ("PNG", (32, 24))
    else:
        assert output_url.startswith("data:image/jpeg;base64,")
        image_format, image_size = _image_format_and_size(output_bytes)
        assert image_format == "JPEG"
        assert image_size[0] < 32
        assert image_size[1] < 24


@pytest.mark.parametrize(
    "url",
    [
        "http://images.example.test/photo.png?token=exact",
        "https://images.example.test/photo.png?token=exact",
    ],
)
def test_prepare_inline_images_preserves_remote_urls_without_opening_them(monkeypatch, url):
    def fail_image_open(*args, **kwargs):
        raise AssertionError("remote image URLs must not be opened locally")

    monkeypatch.setattr("app.core.utils.message_assembler.Image.open", fail_image_open)
    source_part = ImagePart(image_url={"url": url, "detail": "low"})
    message = InternalMessage(role=MessageRole.USER, content=[source_part])
    source_snapshot = message.model_dump(mode="json")

    result = MessageAssembler.prepare_inline_images(
        message,
        ToolConfig(image_compression_enabled=True, image_compression_threshold_kb=1),
    )

    assert result.content[0].image_url == {"url": url, "detail": "low"}
    assert result.content[0].image_url["url"] == url
    assert message.model_dump(mode="json") == source_snapshot


def test_prepare_inline_images_only_compresses_original_user_prefix(inline_png_bytes):
    source_url = _inline_data_url(inline_png_bytes, "percent")
    generated_attachment_url = _inline_data_url(inline_png_bytes, "base64")
    message = InternalMessage(
        role=MessageRole.USER,
        content=[
            ImagePart(image_url={"url": source_url, "detail": "original"}),
            TextPart(text="正文"),
            ImagePart(image_url={"url": generated_attachment_url, "detail": "generated"}),
        ],
        attachments=["assembled.png"],
        assembled_attachment_part_count=1,
    )
    source_snapshot = message.model_dump(mode="json")
    generated_part_snapshot = message.content[-1].model_dump(mode="json")

    prepared = MessageAssembler.prepare_inline_images(
        message,
        ToolConfig(image_compression_enabled=True, image_compression_threshold_kb=1),
    )

    assert message.model_dump(mode="json") == source_snapshot
    assert prepared is not message
    assert prepared.content[1].text == "正文"
    assert prepared.content[2].model_dump(mode="json") == generated_part_snapshot
    assert prepared.content[2].image_url["detail"] == "generated"
    generated_bytes = _decode_inline_data_url(prepared.content[2].image_url["url"])
    assert generated_bytes == inline_png_bytes
    assert _image_format_and_size(generated_bytes) == ("PNG", (32, 24))

    compressed_url = prepared.content[0].image_url["url"]
    assert compressed_url.startswith("data:image/jpeg;base64,")
    compressed_format, compressed_size = _image_format_and_size(_decode_inline_data_url(compressed_url))
    assert compressed_format == "JPEG"
    assert compressed_size[0] < 32
    assert compressed_size[1] < 24
    assert prepared.content[0].image_url["detail"] == "original"

    assistant_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=[ImagePart(image_url={"url": source_url, "detail": "assistant"})],
    )
    assistant_snapshot = assistant_message.model_dump(mode="json")
    assistant_result = MessageAssembler.prepare_inline_images(
        assistant_message,
        ToolConfig(image_compression_enabled=True, image_compression_threshold_kb=1),
    )
    assert assistant_result is assistant_message
    assert assistant_message.model_dump(mode="json") == assistant_snapshot


@pytest.mark.parametrize(
    "url",
    [
        "data:image/png;base64",
        "data:image/png;base64,%%%",
        f"data:image/png;base64,{base64.b64encode(b'not-image' * 300).decode('ascii')}",
    ],
    ids=["missing-comma", "invalid-base64", "non-image-bytes"],
)
def test_prepare_inline_images_raises_business_error_without_mutating_invalid_input(url):
    source_part = ImagePart(image_url={"url": url, "detail": "high"})
    message = InternalMessage(role=MessageRole.USER, content=[source_part])
    source_snapshot = message.model_dump(mode="json")
    source_part_snapshot = source_part.model_dump(mode="json")

    with pytest.raises(ParameterException) as error:
        MessageAssembler.prepare_inline_images(
            message,
            ToolConfig(image_compression_enabled=True, image_compression_threshold_kb=1),
        )

    assert error.value.message == ERR_LLM_IMAGE_INPUT_INVALID
    assert error.value.code == 400
    assert message.model_dump(mode="json") == source_snapshot
    assert source_part.model_dump(mode="json") == source_part_snapshot
