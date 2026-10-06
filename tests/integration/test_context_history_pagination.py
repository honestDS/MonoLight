import json

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import context as context_module
from app.core.context import ContextManager
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
    assert provider_image_url.startswith("data:image/jpeg;base64,")
    assert str(image_path) not in provider_image_url

    await db_session.refresh(persisted_user_message)
    assert persisted_user_message.content == "修改这张图片"
    assert persisted_user_message.attachments == attachment_paths
