import json

from app.core.context import ContextManager
from app.core.dispatchers.background import BackgroundDispatcherMixin
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType


def test_background_task_result_is_preserved_as_new_user_trigger():
    result_content = json.dumps(
        {
            "type": "background_tool_result",
            "instruction": "Proactively reply to the user.",
            "task": {
                "status": "succeeded",
                "tool_name": "generate_image",
                "files": ["generated.png"],
            },
        },
        ensure_ascii=False,
    )
    raw_messages = [
        Message(
            id=3,
            session_id="s1",
            uid="u1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.BACKGROUND_TASK_RESULT,
            content=result_content,
            is_processed=True,
        ),
        Message(
            id=2,
            session_id="s1",
            uid="u1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="任务已提交。",
            is_processed=True,
        ),
        Message(
            id=1,
            session_id="s1",
            uid="u1",
            profile_id=1,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content="用后台模式画一张图。",
            is_processed=True,
        ),
    ]

    parsed = parse_db_messages_to_internal(raw_messages)
    audited = ContextManager.audit_tool_chain(
        list(reversed(parsed)),
        uid="u1",
        session_id="s1",
    )

    assert [message.role for message in audited] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.USER,
    ]
    assert audited[-1].content == result_content


def test_virtual_tool_feedback_builds_matched_tool_chain():
    ai_msg = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=[
            InternalToolCall(
                id="call_f2b01bffa68245b99de77cc1",
                name="generate_image",
                arguments={"prompt": "cat"},
            )
        ],
    )

    messages = BackgroundDispatcherMixin._build_virtual_tool_feedback_messages(
        ai_msg,
        {
            "type": "background_proactive_tool_correction",
            "error": "Unsupported tool call in background proactive reply.",
            "unsupported_tool_calls": ["generate_image"],
            "allowed_tool_calls": ["query_knowledge_base", "send_file_to_user"],
        },
    )

    assert messages[0] is ai_msg
    assert len(messages) == 2
    assert messages[1].role == MessageRole.TOOL
    assert messages[1].tool_call_id == "call_f2b01bffa68245b99de77cc1"

    payload = json.loads(messages[1].content)
    assert payload["type"] == "background_proactive_tool_correction"
    assert payload["unsupported_tool_calls"] == ["generate_image"]
    assert payload["tool_call"] == {
        "id": "call_f2b01bffa68245b99de77cc1",
        "name": "generate_image",
        "arguments": {"prompt": "cat"},
    }

    audited = ContextManager.audit_tool_chain([InternalMessage(role=MessageRole.USER, content="start"), *messages], uid="u1", session_id="s1")
    assert audited == [audited[0], *messages]


def test_audit_tool_chain_drops_tool_call_without_user_or_tool_predecessor():
    orphan_ai_msg = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=[
            InternalToolCall(
                id="call_orphan",
                name="query_knowledge_base",
                arguments={"query": "context"},
            )
        ],
    )
    tool_msg = InternalMessage(
        role=MessageRole.TOOL,
        content=json.dumps({"result": "ok"}, ensure_ascii=False),
        tool_call_id="call_orphan",
    )

    audited = ContextManager.audit_tool_chain([orphan_ai_msg, tool_msg], uid="u1", session_id="s1")

    assert audited == []


def test_audit_tool_chain_keeps_tool_call_after_function_response_turn():
    first_ai_msg = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=[
            InternalToolCall(
                id="call_first",
                name="query_knowledge_base",
                arguments={"query": "context"},
            )
        ],
    )
    first_tool_msg = InternalMessage(
        role=MessageRole.TOOL,
        content=json.dumps({"result": "first"}, ensure_ascii=False),
        tool_call_id="call_first",
    )
    second_ai_msg = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=[
            InternalToolCall(
                id="call_second",
                name="query_knowledge_base",
                arguments={"query": "follow up"},
            )
        ],
    )
    second_tool_msg = InternalMessage(
        role=MessageRole.TOOL,
        content=json.dumps({"result": "second"}, ensure_ascii=False),
        tool_call_id="call_second",
    )

    user_msg = InternalMessage(role=MessageRole.USER, content="start")
    audited = ContextManager.audit_tool_chain(
        [
            user_msg,
            first_ai_msg,
            first_tool_msg,
            second_ai_msg,
            second_tool_msg,
        ],
        uid="u1",
        session_id="s1",
    )

    assert audited == [
        user_msg,
        first_ai_msg,
        first_tool_msg,
        second_ai_msg,
        second_tool_msg,
    ]


def test_virtual_tool_feedback_returns_empty_without_tool_calls():
    ai_msg = InternalMessage(role=MessageRole.ASSISTANT, content="plain text")

    messages = BackgroundDispatcherMixin._build_virtual_tool_feedback_messages(
        ai_msg,
        {"type": "background_proactive_tool_correction"},
    )

    assert messages == []


def test_virtual_tool_feedback_keeps_system_messages_at_beginning_after_trim():
    system_msg = InternalMessage(role=MessageRole.SYSTEM, content="system prompt")
    user_msg = InternalMessage(role=MessageRole.USER, content="background task context")
    ai_msg = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        tool_calls=[
            InternalToolCall(
                id="call_f2b01bffa68245b99de77cc1",
                name="generate_image",
                arguments={},
            )
        ],
    )
    correction_messages = BackgroundDispatcherMixin._build_virtual_tool_feedback_messages(
        ai_msg,
        {
            "type": "background_proactive_tool_correction",
            "error": "Unsupported tool call in background proactive reply.",
            "unsupported_tool_calls": ["generate_image"],
        },
    )

    trimmed = ContextManager.trim_messages_for_model_request(
        messages=[system_msg, user_msg, *correction_messages],
        uid="5c201bbc61a845dfbf728ca64de91497",
        session_id="04e23f54-4a32-4f91-8f90-858464c99657",
        context_window_k=4,
        max_tokens=256,
        tools=None,
    )

    system_indexes = [idx for idx, message in enumerate(trimmed) if message.role == MessageRole.SYSTEM]
    assert system_indexes == [0]
    assert all(message.role != MessageRole.SYSTEM for message in trimmed[1:])
    assert [message.role for message in trimmed[-2:]] == [MessageRole.ASSISTANT, MessageRole.TOOL]
    assert trimmed[-1].tool_call_id == "call_f2b01bffa68245b99de77cc1"
