import json

from PIL import Image

from app.core.dispatchers.interactive_helpers import (
    build_pending_multimodal_input_message,
    collect_pending_multimodal_file_inputs,
)
from app.core.utils.message_assembler import MessageAssembler
from app.models.message import InternalMessage, InternalToolCall, MessageRole


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
