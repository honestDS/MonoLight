import json

import pytest
from PIL import Image

from app.core.dispatchers.interactive_helpers import (
    build_pending_multimodal_input_message,
    collect_pending_multimodal_file_inputs,
)
from app.core.utils.message_assembler import MessageAssembler
from app.models.message import InternalMessage, InternalToolCall, MessageRole, TextPart


def _success_result(path, message="不是用户的新输入", modality="image"):
    return json.dumps(
        {
            "type": "multimodal_file_read",
            "status": "success",
            "modality": modality,
            "path": str(path.resolve()),
            "message": message,
        },
        ensure_ascii=False,
    )


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
            content=json.dumps({"type": "multimodal_file_read", "status": "failed", "error": "failed"}),
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
    assert all(part.image_url["url"].startswith("data:image/jpeg;base64,") for part in image_parts)
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


def test_message_assembler_replaces_old_per_image_history_wrappers_after_restore():
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
