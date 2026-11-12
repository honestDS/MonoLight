import base64
import json

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import context as context_module
from app.core.constants import ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER
from app.core.context import ContextManager
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.i18n import t
from app.core.utils.context_summary.common import serialize_message
from app.core.utils.dispatcher.frozen_context import capture_frozen_context_messages, load_frozen_context_messages
from app.core.utils.dispatcher.helpers import reassemble_multimodal_messages
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer


def _message(
    message_id: int,
    role: MessageRole,
    *,
    content: str | None = None,
    message_type: MessageType = MessageType.TEXT,
) -> Message:
    return Message(
        id=message_id,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=role,
        type=message_type,
        content=content if content is not None else f"message-{message_id}",
        is_processed=True,
    )


def _profile() -> Profile:
    return Profile(id=1, uid="user-1", name="test-profile", configs={})


class CapturingLogger:
    def __init__(self):
        self.warning_messages: list[str] = []

    def bind(self, **_kwargs):
        return self

    def warning(self, message):
        self.warning_messages.append(str(message))

    def info(self, _message):
        return None


@pytest.mark.asyncio
async def test_context_history_reads_more_than_five_thousand_messages(db_session: AsyncSession):
    history_message_count = 5006
    db_session.add_all(
        [
            _message(
                message_id,
                MessageRole.USER if message_id % 2 == 1 else MessageRole.ASSISTANT,
                content="",
            )
            for message_id in range(1, history_message_count + 1)
        ]
    )
    db_session.add(
        _message(
            history_message_count + 1,
            MessageRole.USER,
            content="current input",
        )
    )
    await db_session.commit()

    messages = await ContextManager.get_messages(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=_profile(),
        current_message="current input",
        before_id=history_message_count + 1,
        context_window_k=8,
    )

    assert len(messages) == history_message_count
    assert [message.id for message in messages[:3]] == [1, 2, 3]
    assert [message.id for message in messages[-3:]] == [5004, 5005, 5006]


@pytest.mark.asyncio
async def test_context_history_respects_fixed_id_bounds(db_session: AsyncSession):
    db_session.add_all(
        [
            _message(
                message_id,
                MessageRole.USER if message_id % 2 == 1 else MessageRole.ASSISTANT,
            )
            for message_id in range(1, 11)
        ]
    )
    await db_session.commit()

    messages = await ContextManager.get_messages(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=_profile(),
        current_message="current input",
        before_id=9,
        after_id=3,
        context_window_k=4,
    )

    assert [message.id for message in messages] == [4, 5, 6, 7, 8]


@pytest.mark.asyncio
async def test_context_history_keeps_tool_chain_complete_across_backward_pages(
    db_session: AsyncSession,
):
    tool_call_content = json.dumps(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "name": "query_knowledge_base",
                    "arguments": {"query": "pagination"},
                }
            ],
        },
        ensure_ascii=False,
    )
    tool_result_content = json.dumps(
        {
            "tool_call_id": "call-1",
            "content": json.dumps({"result": "matched"}, ensure_ascii=False),
        },
        ensure_ascii=False,
    )
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content="find context"),
            _message(
                2,
                MessageRole.ASSISTANT,
                content=tool_call_content,
                message_type=MessageType.TOOL_CALL,
            ),
            _message(
                3,
                MessageRole.TOOL,
                content=tool_result_content,
                message_type=MessageType.TOOL_RESULT,
            ),
            _message(4, MessageRole.ASSISTANT, content="final answer"),
        ]
    )
    await db_session.commit()

    raw_history = await ContextManager._load_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        before_id=None,
        after_id=None,
        page_size=2,
    )
    parsed_history = list(reversed(parse_db_messages_to_internal(raw_history)))
    messages = ContextManager.audit_tool_chain(
        parsed_history,
        uid="user-1",
        session_id="session-1",
    )

    assert [message.id for message in messages] == [1, 2, 3, 4]
    assert messages[1].tool_calls is not None
    assert messages[1].tool_calls[0].id == "call-1"
    assert messages[2].tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_context_history_does_not_retruncate_persisted_tool_result(
    db_session: AsyncSession,
):
    tool_call_content = json.dumps(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call-large",
                    "name": "execute_shell",
                    "arguments": {"command": "large output", "execution_mode": "non_interactive"},
                }
            ],
        },
        ensure_ascii=False,
    )
    persisted_result = "stable-result-" * 2000
    tool_result_content = json.dumps(
        {
            "tool_call_id": "call-large",
            "content": persisted_result,
        },
        ensure_ascii=False,
    )
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content="run it"),
            _message(2, MessageRole.ASSISTANT, content=tool_call_content, message_type=MessageType.TOOL_CALL),
            _message(3, MessageRole.TOOL, content=tool_result_content, message_type=MessageType.TOOL_RESULT),
        ]
    )
    await db_session.commit()

    messages = await ContextManager.get_messages(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=_profile(),
        current_message="continue",
        context_window_k=1,
    )

    tool_result = next(message for message in messages if message.role == MessageRole.TOOL)
    assert tool_result.content == persisted_result


def test_tool_audit_still_reports_genuine_orphan_tool_result(
    monkeypatch,
):
    log = CapturingLogger()
    monkeypatch.setattr(context_module, "logger", log)
    orphan_result = parse_db_messages_to_internal(
        [
            _message(
                3,
                MessageRole.TOOL,
                content=json.dumps(
                    {
                        "tool_call_id": "missing-call",
                        "content": "orphan result",
                    }
                ),
                message_type=MessageType.TOOL_RESULT,
            )
        ]
    )

    retained = ContextManager.audit_tool_chain(
        orphan_result,
        uid="user-1",
        session_id="session-1",
    )

    assert retained == []
    assert len(log.warning_messages) == 1
    assert "missing-call" in log.warning_messages[0]


def test_tool_audit_does_not_report_duplicate_known_results_as_orphaned(monkeypatch):
    log = CapturingLogger()
    monkeypatch.setattr(context_module, "logger", log)
    user_message = InternalMessage(role=MessageRole.USER, content="run tool")
    tool_call_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={})],
    )
    tool_call_message.tool_calls.append(InternalToolCall(id="call-1", name="execute_shell", arguments={}))
    first_result = InternalMessage(role=MessageRole.TOOL, tool_call_id="call-1", content="first")
    repeated_result = InternalMessage(role=MessageRole.TOOL, tool_call_id="call-1", content="repeated")

    retained = ContextManager.audit_tool_chain(
        [user_message, tool_call_message, first_result, repeated_result],
        uid="user-1",
        session_id="session-1",
    )

    assert retained == [user_message, tool_call_message, first_result]
    assert log.warning_messages == []


@pytest.mark.asyncio
async def test_context_history_loads_complete_unsummarized_range_without_request_budget_cutoff(
    db_session: AsyncSession,
):
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content="old-user " * 300),
            _message(2, MessageRole.ASSISTANT, content="old-answer " * 300),
            _message(3, MessageRole.USER, content="recent-user " * 300),
            _message(4, MessageRole.ASSISTANT, content="recent-answer " * 300),
            _message(5, MessageRole.USER, content="latest-user " * 300),
            _message(6, MessageRole.ASSISTANT, content="latest-answer " * 300),
        ]
    )
    await db_session.commit()

    raw_history = await ContextManager._load_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        before_id=None,
        after_id=None,
        page_size=2,
    )

    assert [message.id for message in raw_history] == [6, 5, 4, 3, 2, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transformer",
    [OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer],
)
async def test_persisted_history_attachments_keep_paths_and_wrapper_in_provider_request(
    db_session: AsyncSession,
    transformer,
):
    attachment_paths = ["d:/a.jpg", "d:/b.png", "d:/c.mp3", "d:/d.mp4", "d:/e.pdf"]
    persisted_user_message = _message(1, MessageRole.USER, content="历史附件正文")
    persisted_user_message.attachments = attachment_paths
    db_session.add_all(
        [
            persisted_user_message,
            _message(2, MessageRole.ASSISTANT),
            _message(3, MessageRole.USER, content="当前输入"),
        ]
    )
    await db_session.commit()

    messages = await ContextManager.get_messages(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=_profile(),
        current_message="当前输入",
        before_id=3,
    )
    messages.append(InternalMessage(id=3, role=MessageRole.USER, content="当前输入"))
    for _ in range(2):
        reassemble_multimodal_messages(
            messages,
            image_understanding=False,
            audio_understanding=False,
            video_understanding=False,
        )

    provider_messages = transformer.to_provider(materialize_user_environment_prompts(messages))
    history_user_message = json.loads(provider_messages[0]["content"][0]["text"])
    assert history_user_message["attachment_paths"] == attachment_paths
    assert history_user_message["user_message"] == [
        {"type": "text", "text": "历史附件正文"},
        {
            "type": "text",
            "text": "[系统提示,此处不是用户说的话][历史图片：d:/a.jpg][历史图片：d:/b.png][历史音频][未开启视频理解: d:/d.mp4]",
        },
        {"type": "attachment", "index": 0, "media_type": "file"},
        {"type": "text", "text": "[系统提示结束]"},
    ]
    assert provider_messages[0]["content"][1]["text"] == "[Attached File: d:/e.pdf]"
    assert "assembled_attachment_part_count" not in json.dumps(provider_messages, ensure_ascii=False)
    assert json.loads(provider_messages[-1]["content"])["user_message"] == "当前输入"
    assert "attachment_paths" not in json.loads(provider_messages[-1]["content"])

    await db_session.refresh(persisted_user_message)
    assert persisted_user_message.content == "历史附件正文"
    assert persisted_user_message.attachments == attachment_paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transformer",
    [OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer],
)
async def test_persisted_history_real_image_attachment_paths_keep_provider_image_data(
    db_session: AsyncSession,
    transformer,
    tmp_path,
):
    image_path = tmp_path / "真实 图片.png"
    Image.new("RGB", (2, 2), color=(12, 34, 56)).save(image_path)
    attachment_paths = [str(image_path)]
    persisted_user_message = _message(1, MessageRole.USER, content="修改这张图片")
    persisted_user_message.attachments = attachment_paths
    db_session.add(persisted_user_message)
    await db_session.commit()

    messages = await ContextManager.get_messages(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=_profile(),
        current_message="修改这张图片",
    )
    assert len(messages) == 1
    assert messages[0].role == MessageRole.USER
    reassemble_multimodal_messages(
        messages,
        image_understanding=False,
        audio_understanding=False,
        video_understanding=False,
    )
    reassemble_multimodal_messages(
        messages,
        image_understanding=True,
        audio_understanding=False,
        video_understanding=False,
    )
    assembled_image_url = next(part.image_url["url"] for part in messages[0].content if getattr(part, "type", None) == "image_url")

    provider_messages = transformer.to_provider(materialize_user_environment_prompts(messages))
    provider_content = provider_messages[0]["content"]
    payload = json.loads(provider_content[0]["text"])
    assert payload["attachment_paths"] == attachment_paths
    assert payload["user_message"] == [
        {"type": "text", "text": "修改这张图片"},
        {"type": "text", "text": "[系统提示,此处不是用户说的话]"},
        {"type": "attachment", "index": 0, "media_type": "image_url"},
        {"type": "text", "text": "[系统提示结束]"},
    ]
    assert len(provider_content) == 2
    assert provider_content[0]["type"] in {"text", "input_text"}
    if transformer is OpenAIChatCompletionsTransformer:
        assert provider_content[1]["type"] == "image_url"
        provider_image_url = provider_content[1]["image_url"]["url"]
    else:
        assert provider_content[1]["type"] == "input_image"
        provider_image_url = provider_content[1]["image_url"]
    assert provider_image_url == assembled_image_url
    assert provider_image_url.startswith("data:image/png;base64,")
    assert base64.b64decode(provider_image_url.split(",", 1)[1]) == image_path.read_bytes()
    assert str(image_path) not in provider_image_url

    await db_session.refresh(persisted_user_message)
    assert persisted_user_message.content == "修改这张图片"
    assert persisted_user_message.attachments == attachment_paths


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_status", ["blocked", "audit_failed", "pending", "cancelled"])
@pytest.mark.parametrize(
    "transformer",
    [OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer],
)
async def test_persisted_audit_tool_feedback_is_obscured_from_provider_request(
    db_session: AsyncSession,
    audit_status: str,
    transformer,
):
    tool_call_id = "call-audit"
    private_markers = [
        "AUDIT_REASON_PRIVATE_MARKER",
        "AUDIT_SCORE_PRIVATE_MARKER",
        "AUDIT_DENY_PRIVATE_MARKER",
        "AUDIT_FILE_CHECKS_PRIVATE_MARKER",
        "AUDIT_REASONING_PRIVATE_MARKER",
        "AUDIT_REFUSAL_PRIVATE_MARKER",
        "AUDIT_PROVIDER_METADATA_PRIVATE_MARKER",
        "AUDIT_CARD_REASON_PRIVATE_MARKER",
        "AUDIT_CARD_SCORE_PRIVATE_MARKER",
        "AUDIT_CARD_DENY_PRIVATE_MARKER",
        "AUDIT_CARD_FILE_CHECKS_PRIVATE_MARKER",
    ]
    audit_payload = {
        "status": audit_status,
        "reason": private_markers[0],
        "score": private_markers[1],
        "deny": private_markers[2],
        "file_checks": [private_markers[3]],
        "reasoning": private_markers[4],
        "refusal": private_markers[5],
        "provider_metadata": {"private": private_markers[6]},
    }
    if audit_status == "cancelled":
        audit_payload.update(
            {
                "work_id": 987654321,
                "subagent_id": "AUDIT_SUBAGENT_ID_PRIVATE_MARKER",
            }
        )
        private_markers.extend([str(audit_payload["work_id"]), audit_payload["subagent_id"]])
    persisted_tool_result_content = json.dumps(
        {
            "tool_call_id": tool_call_id,
            "content": json.dumps(audit_payload, ensure_ascii=False),
            "refusal": private_markers[5],
        },
        ensure_ascii=False,
    )
    persisted_tool_result = _message(
        3,
        MessageRole.TOOL,
        content=persisted_tool_result_content,
        message_type=MessageType.TOOL_RESULT,
    )
    persisted_tool_result.reasoning_content = private_markers[4]
    persisted_tool_result.provider_metadata = {"private": private_markers[6]}
    confirmation_content = json.dumps(
        {
            "status": audit_status,
            "reason": private_markers[7],
            "score": private_markers[8],
            "deny": private_markers[9],
            "file_checks": [private_markers[10]],
            "confirmation_decision": "approve",
        },
        ensure_ascii=False,
    )
    persisted_confirmation = _message(
        4,
        MessageRole.USER,
        content=confirmation_content,
        message_type=MessageType.AUDIT_CONFIRMATION,
    )
    db_session.add_all(
        [
            _message(1, MessageRole.USER, content="run audited tool"),
            _message(
                2,
                MessageRole.ASSISTANT,
                content=json.dumps(
                    {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tool_call_id,
                                "name": "execute_shell",
                                "arguments": {"command": "audited operation"},
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                message_type=MessageType.TOOL_CALL,
            ),
            persisted_tool_result,
            persisted_confirmation,
            _message(5, MessageRole.USER, content="current input"),
        ]
    )
    await db_session.commit()

    messages = await ContextManager.get_messages(
        db_session,
        session_id="session-1",
        uid="user-1",
        profile=_profile(),
        current_message="current input",
        before_id=5,
    )
    assert [message.id for message in messages] == [1, 2, 3]
    messages.append(InternalMessage(id=5, role=MessageRole.USER, content="current input"))

    tool_call = next(message for message in messages if message.tool_calls)
    tool_result = next(message for message in messages if message.role == MessageRole.TOOL)
    assert tool_call.tool_calls[0].id == tool_result.tool_call_id == tool_call_id

    provider_messages = transformer.to_provider(messages)
    provider_request = json.dumps(provider_messages, ensure_ascii=False)
    assert "current input" in provider_request
    assert provider_request.count(tool_call_id) == 2
    for marker in private_markers:
        assert marker not in provider_request

    if transformer is OpenAIChatCompletionsTransformer:
        provider_tool_result = next(item for item in provider_messages if item.get("role") == "tool")
        provider_result_content = provider_tool_result["content"]
    else:
        provider_tool_result = next(item for item in provider_messages if item.get("type") == "function_call_output")
        provider_result_content = provider_tool_result["output"]
    provider_audit_payload = json.loads(provider_result_content)
    assert set(provider_audit_payload) == {"status", "error"}
    assert provider_audit_payload["status"] == audit_status
    if audit_status == "audit_failed":
        assert "不可用" in provider_audit_payload["error"]
        assert "不可用" in provider_request

    await db_session.refresh(persisted_tool_result)
    await db_session.refresh(persisted_confirmation)
    assert persisted_tool_result.content == persisted_tool_result_content
    assert persisted_tool_result.reasoning_content == private_markers[4]
    assert persisted_tool_result.provider_metadata == {"private": private_markers[6]}
    assert persisted_confirmation.content == confirmation_content


@pytest.mark.parametrize("include_rejection_source", [False, True])
@pytest.mark.parametrize(
    "transformer",
    [OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer],
)
def test_prepare_messages_for_model_request_obscures_legacy_audit_checkpoint_copy_only(
    include_rejection_source: bool,
    transformer,
):
    audit_payload = {
        "status": "rejected",
        "reason": "AUDIT_PAYLOAD_REASON_PRIVATE_MARKER",
        "reasoning": "AUDIT_PAYLOAD_REASONING_PRIVATE_MARKER",
        "score": "AUDIT_PAYLOAD_SCORE_PRIVATE_MARKER",
        "deny": "AUDIT_PAYLOAD_DENY_PRIVATE_MARKER",
        "file_checks": ["AUDIT_PAYLOAD_FILE_CHECKS_PRIVATE_MARKER"],
        "refusal": "AUDIT_PAYLOAD_REFUSAL_PRIVATE_MARKER",
        "provider_metadata": {"private": "AUDIT_PAYLOAD_PROVIDER_METADATA_PRIVATE_MARKER"},
        "confirmation_status": "reject",
        "confirmation_decision": "拒绝",
    }
    if include_rejection_source:
        audit_payload["rejection_source"] = "user"
    messages = [
        InternalMessage(role=MessageRole.USER, content="user checkpoint text"),
        InternalMessage(role=MessageRole.ASSISTANT, content="assistant checkpoint text"),
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="call-audit",
            content=json.dumps(audit_payload, ensure_ascii=False),
            reasoning_content="AUDIT_MESSAGE_REASONING_PRIVATE_MARKER",
            refusal="AUDIT_MESSAGE_REFUSAL_PRIVATE_MARKER",
            provider_metadata={"private": "AUDIT_MESSAGE_PROVIDER_METADATA_PRIVATE_MARKER"},
        ),
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="call-business-success",
            content=json.dumps({"status": "succeeded", "result": "BUSINESS_SUCCESS_MARKER"}),
        ),
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id="call-business-failure",
            content=json.dumps(
                {
                    "status": "failed",
                    "error": "BUSINESS_FAILURE_MARKER",
                    "reason": "BUSINESS_FAILURE_REASON_MARKER",
                }
            ),
        ),
    ]
    originals = [message.model_copy(deep=True) for message in messages]

    prepared = ContextManager.prepare_messages_for_model_request(
        messages,
        context_window_k=128,
        max_tokens=1,
        safety_margin_tokens=0,
    )

    assert messages == originals
    assert all(prepared_message is not original for prepared_message, original in zip(prepared, messages))
    assert prepared[0].content == "user checkpoint text"
    assert prepared[1].content == "assistant checkpoint text"
    assert prepared[3:] == originals[3:]

    prepared_audit_payload = json.loads(prepared[2].content)
    expected_audit_keys = {
        "status",
        "reason",
        "confirmation_status",
        "confirmation_decision",
    }
    if include_rejection_source:
        expected_audit_keys.add("rejection_source")
    assert set(prepared_audit_payload) == expected_audit_keys | {"error"}
    assert prepared_audit_payload["status"] == "rejected"
    rejection_message = prepared_audit_payload["reason"]
    assert rejection_message == t(ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER)
    assert prepared_audit_payload["error"] == rejection_message
    assert prepared_audit_payload["confirmation_status"] == "reject"
    assert prepared_audit_payload["confirmation_decision"] == "拒绝"
    if include_rejection_source:
        assert prepared_audit_payload["rejection_source"] == "user"
    else:
        assert "rejection_source" not in prepared_audit_payload
    assert prepared[2].reasoning_content is None
    assert prepared[2].refusal is None
    assert prepared[2].provider_metadata is None

    provider_messages = transformer.to_provider(prepared)
    if transformer is OpenAIChatCompletionsTransformer:
        provider_tool_result = next(item for item in provider_messages if item.get("role") == "tool")
        provider_result_content = provider_tool_result["content"]
    else:
        provider_tool_result = next(item for item in provider_messages if item.get("type") == "function_call_output")
        provider_result_content = provider_tool_result["output"]
    provider_audit_payload = json.loads(provider_result_content)
    assert provider_audit_payload == prepared_audit_payload

    prepared_json = json.dumps([message.model_dump(mode="json") for message in prepared], ensure_ascii=False)
    provider_json = json.dumps(provider_messages, ensure_ascii=False)
    for marker in (
        "AUDIT_PAYLOAD_REASON_PRIVATE_MARKER",
        "AUDIT_PAYLOAD_REASONING_PRIVATE_MARKER",
        "AUDIT_PAYLOAD_SCORE_PRIVATE_MARKER",
        "AUDIT_PAYLOAD_DENY_PRIVATE_MARKER",
        "AUDIT_PAYLOAD_FILE_CHECKS_PRIVATE_MARKER",
        "AUDIT_PAYLOAD_REFUSAL_PRIVATE_MARKER",
        "AUDIT_PAYLOAD_PROVIDER_METADATA_PRIVATE_MARKER",
        "AUDIT_MESSAGE_REASONING_PRIVATE_MARKER",
        "AUDIT_MESSAGE_REFUSAL_PRIVATE_MARKER",
        "AUDIT_MESSAGE_PROVIDER_METADATA_PRIVATE_MARKER",
    ):
        assert marker not in prepared_json
        assert marker not in provider_json
    assert "BUSINESS_SUCCESS_MARKER" in prepared_json
    assert "BUSINESS_FAILURE_MARKER" in prepared_json
    assert "BUSINESS_FAILURE_REASON_MARKER" in prepared_json
    assert "BUSINESS_SUCCESS_MARKER" in provider_json
    assert "BUSINESS_FAILURE_MARKER" in provider_json
    assert "BUSINESS_FAILURE_REASON_MARKER" in provider_json


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["OPENAI", "OPENAI_RESPONSES"])
async def test_sent_file_history_isolated_across_same_work_reload_and_frozen_context(
    image_chat_runtime,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    protocol: str,
):
    original_assistant_text = "原文字回复"
    business_json = json.dumps(
        {"status": "succeeded", "result": "普通业务JSON正文"},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    file_name = "sent-report.txt"
    file_path = tmp_path / file_name
    file_bytes = b"sent attachment body"
    file_path.write_bytes(file_bytes)
    script_tool_call_id = "send-file-call"

    async with image_chat_runtime(protocol, goal_mode=False) as runtime:
        runtime.outputs.extend(
            [
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id=script_tool_call_id,
                            name="send_file_to_user",
                            arguments={
                                "files": [
                                    {
                                        "path": str(file_path),
                                        "display_name": file_name,
                                        "mime_type": "text/plain",
                                    }
                                ]
                            },
                        )
                    ],
                ),
                InternalMessage(role=MessageRole.ASSISTANT, content=original_assistant_text),
                InternalMessage(role=MessageRole.ASSISTANT, content=business_json),
            ]
        )

        transformer = OpenAIChatCompletionsTransformer if protocol == "OPENAI" else OpenAIResponsesTransformer
        fixture_post_json = transformer._post_json
        follow_up_submission = None

        async def wrapped_post_json(_transformer, **kwargs):
            nonlocal follow_up_submission
            if len(runtime.provider_payloads) + 1 == 2:
                follow_up = await runtime.client.post(
                    "/api/v1/chat/completions",
                    json={
                        "session_id": runtime.session_id,
                        "request_id": "same-work-follow-up",
                        "message": "同一工作追问",
                    },
                )
                assert follow_up.status_code == 200
                follow_up_submission = follow_up.json()["data"]
            return await fixture_post_json(_transformer, **kwargs)

        monkeypatch.setattr(transformer, "_post_json", wrapped_post_json)

        accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "send-file-request",
                "message": "发送文件并继续",
            },
        )
        assert accepted.status_code == 200
        accepted_data = accepted.json()["data"]
        initial_work_id = accepted_data["work_id"]

        completed = await runtime.run_next()
        assert completed is not None and completed.id == initial_work_id and completed.status.value == "succeeded"
        assert follow_up_submission is not None
        assert follow_up_submission["submission_status"] == "queued"
        follow_up_work_id = follow_up_submission["work_id"]
        assert follow_up_work_id != initial_work_id
        assert len(runtime.provider_payloads) == 3

        if protocol == "OPENAI":
            actual_tool_call = next(
                tool_call for item in runtime.provider_payloads[1]["messages"] if item.get("role") == MessageRole.ASSISTANT.value for tool_call in item.get("tool_calls") or [] if isinstance(tool_call, dict) and isinstance(tool_call.get("function"), dict) and tool_call["function"].get("name") == "send_file_to_user"
            )
            tool_call_id = actual_tool_call["id"]
        else:
            actual_tool_call = next(item for item in runtime.provider_payloads[1]["input"] if item.get("type") == "function_call" and item.get("name") == "send_file_to_user")
            tool_call_id = actual_tool_call.get("call_id") or actual_tool_call.get("id")
        assert isinstance(tool_call_id, str) and tool_call_id

        reply_response = await runtime.client.get(f"/api/v1/chat/reply-works/{initial_work_id}")
        assert reply_response.status_code == 200
        work_data = reply_response.json()["data"]
        assert work_data["status"] == "succeeded"
        follow_up_reply = await runtime.client.get(f"/api/v1/chat/reply-works/{follow_up_work_id}")
        assert follow_up_reply.status_code == 200
        assert follow_up_reply.json()["data"]["resolved_work_id"] == initial_work_id
        response = work_data["response"]
        response_files = response["files"]
        assert isinstance(response_files, list) and len(response_files) == 1
        response_file = response_files[0]
        assert response_file["name"] == file_name
        assert response_file["mime_type"] == "text/plain"
        response_content = json.loads(response["choices"][0]["message"]["content"])
        assert response_content == {
            "type": "assistant_files",
            "text": business_json,
            "files": response_files,
        }
        download = await runtime.client.get(response_file["download_url"])
        assert download.status_code == 200
        assert download.content == file_bytes

        history_response = await runtime.client.get(
            "/api/v1/chat/sessions/history",
            params={"session_id": runtime.session_id},
        )
        assert history_response.status_code == 200
        history = history_response.json()["data"]
        assistant_file_cards = []
        for item in history:
            if item.get("role") != MessageRole.ASSISTANT.value or not isinstance(item.get("content"), str):
                continue
            try:
                content = json.loads(item["content"])
            except json.JSONDecodeError:
                continue
            if isinstance(content, dict) and content.get("type") == "assistant_files":
                assistant_file_cards.append(content)
        assert any(card["text"] == original_assistant_text and card["files"] == response_files for card in assistant_file_cards)
        assert any(card["text"] == business_json and card["files"] == response_files for card in assistant_file_cards)

        def provider_items(payload):
            return payload["messages"] if protocol == "OPENAI" else payload["input"]

        def provider_assistant_texts(payload):
            texts = []
            for item in provider_items(payload):
                if item.get("role") != MessageRole.ASSISTANT.value:
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    texts.extend(part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
            return texts

        def provider_tool_result(payload):
            if protocol == "OPENAI":
                item = next(item for item in provider_items(payload) if item.get("role") == MessageRole.TOOL.value and item.get("tool_call_id") == tool_call_id)
                return item["content"]
            item = next(item for item in provider_items(payload) if item.get("type") == "function_call_output" and item.get("call_id") == tool_call_id)
            return item["output"]

        sent_token = response_file["id"]
        sent_download_url = response_file["download_url"]

        def assert_isolated_provider_payload(payload):
            provider_json = json.dumps(payload, ensure_ascii=False)
            assert original_assistant_text in provider_assistant_texts(payload)
            assert "assistant_files" not in provider_json
            assert sent_token not in provider_json
            assert sent_download_url not in provider_json
            visible_tool_result = json.loads(provider_tool_result(payload))
            assert visible_tool_result["status"] == "success"
            assert visible_tool_result["files"][0]["name"] == file_name
            assert "type" not in visible_tool_result
            assert "type" not in visible_tool_result["files"][0]
            assert "id" not in visible_tool_result["files"][0]
            assert "download_url" not in visible_tool_result["files"][0]
            assert "url" not in visible_tool_result["files"][0]

        assert_isolated_provider_payload(runtime.provider_payloads[2])

        async with runtime.factory() as db:
            stored_rows = list((await db.execute(select(Message).where(Message.session_id == runtime.session_id).order_by(Message.id))).scalars().all())
            stored_snapshot = [(row.id, row.content, row.attachments) for row in stored_rows]
            latest_message_id = max(row.id for row in stored_rows if row.id is not None)
            frozen_serialized = await capture_frozen_context_messages(
                db,
                session_id=runtime.session_id,
                uid=runtime.uid,
                before_id=latest_message_id + 1,
            )

            frozen_messages = load_frozen_context_messages(frozen_serialized)
            assert frozen_messages is not None
            frozen_snapshot = [message.model_dump(mode="json") for message in frozen_messages]
            frozen_assistant_texts = [message.content for message in frozen_messages if message.role == MessageRole.ASSISTANT and not message.tool_calls]
            assert original_assistant_text in frozen_assistant_texts
            frozen_tool = next(message for message in frozen_messages if message.tool_call_id == tool_call_id)
            frozen_tool_payload = json.loads(frozen_tool.content or "{}")
            assert frozen_tool_payload["files"][0]["id"] == sent_token

            prepared_messages = ContextManager.prepare_messages_for_model_request(
                frozen_messages,
                context_window_k=32,
                max_tokens=512,
                safety_margin_tokens=0,
            )
            assert [message.model_dump(mode="json") for message in frozen_messages] == frozen_snapshot
            prepared_assistant_texts = [message.content for message in prepared_messages if message.role == MessageRole.ASSISTANT and not message.tool_calls]
            assert original_assistant_text in prepared_assistant_texts
            prepared_tool = next(message for message in prepared_messages if message.tool_call_id == tool_call_id)
            prepared_tool_payload = json.loads(prepared_tool.content or "{}")
            assert prepared_tool_payload["status"] == "success"
            assert prepared_tool_payload["files"][0]["name"] == file_name
            assert "type" not in prepared_tool_payload
            assert sent_token not in json.dumps(prepared_tool_payload, ensure_ascii=False)
            assert sent_download_url not in json.dumps(prepared_tool_payload, ensure_ascii=False)

            serialized_context = "\n".join(serialize_message(message) for message in frozen_messages)
            assert original_assistant_text in serialized_context
            assert "assistant_files" not in serialized_context
            assert sent_token not in serialized_context
            assert sent_download_url not in serialized_context

            checkpoint = await session_reply_work_item_crud.get_checkpoint(db, initial_work_id)
            assert checkpoint is not None
            checkpoint_messages = checkpoint["messages"]
            checkpoint_tool_call_ids = [tool_call.get("id") for item in checkpoint_messages if item.get("role") == MessageRole.ASSISTANT.value for tool_call in item.get("tool_calls") or []]
            checkpoint_tool_result_ids = [item.get("tool_call_id") for item in checkpoint_messages if item.get("role") == MessageRole.TOOL.value]
            assert tool_call_id in checkpoint_tool_call_ids
            assert tool_call_id in checkpoint_tool_result_ids
            checkpoint_assistant_contents = [item.get("content") for item in checkpoint_messages if item.get("role") == MessageRole.ASSISTANT.value and not item.get("tool_calls")]
            assert original_assistant_text in checkpoint_assistant_contents
            assert all("assistant_files" not in str(content) for content in checkpoint_assistant_contents)

        async with runtime.factory() as db:
            rows_after_context_views = list((await db.execute(select(Message).where(Message.session_id == runtime.session_id).order_by(Message.id))).scalars().all())
        assert [(row.id, row.content, row.attachments) for row in rows_after_context_views] == stored_snapshot

        runtime.outputs.append(InternalMessage(role=MessageRole.ASSISTANT, content=business_json))
        reload_accepted = await runtime.client.post(
            "/api/v1/chat/completions",
            json={
                "session_id": runtime.session_id,
                "request_id": "reload-history-request",
                "message": "重载历史后继续",
            },
        )
        assert reload_accepted.status_code == 200
        reload_work_id = reload_accepted.json()["data"]["work_id"]
        assert reload_work_id != initial_work_id
        reloaded = await runtime.run_next()
        assert reloaded is not None and reloaded.id == reload_work_id and reloaded.status.value == "succeeded"
        assert len(runtime.provider_payloads) == 4
        assert_isolated_provider_payload(runtime.provider_payloads[3])

        reload_reply = await runtime.client.get(f"/api/v1/chat/reply-works/{reload_work_id}")
        assert reload_reply.status_code == 200
        reload_response = reload_reply.json()["data"]["response"]
        assert reload_response["choices"][0]["message"]["content"] == business_json
        assert reload_response["files"] in (None, [])

        async with runtime.factory() as db:
            final_rows = list((await db.execute(select(Message).where(Message.session_id == runtime.session_id, Message.role == MessageRole.ASSISTANT).order_by(Message.id))).scalars().all())
        assert final_rows[-1].content == business_json
