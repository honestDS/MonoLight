import json

import pytest

from app.core.constants import CONTEXT_WINDOW_TOKENS_PER_K, ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED
from app.core.context import ContextManager
from app.core.exceptions import ParameterException
from app.core.utils.context_budget import measure_context_request_usage
from app.core.utils.context_messages import message_token_text
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import ImagePart, InternalMessage, InternalToolCall, MessageRole, TextPart


def test_complete_request_usage_counts_messages_tools_output_and_safety_with_shared_token_text():
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system prompt\nruntime instruction"),
        InternalMessage(
            id=1,
            role=MessageRole.USER,
            content=[
                TextPart(text="describe the image"),
                ImagePart(image_url={"url": "data:image/png;base64,large-provider-payload"}),
            ],
        ),
        InternalMessage(
            id=2,
            role=MessageRole.ASSISTANT,
            tool_calls=[
                InternalToolCall(
                    id="call-1",
                    name="lookup",
                    arguments={"query": "important value"},
                )
            ],
        ),
    ]
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]

    usage = measure_context_request_usage(
        messages=messages,
        context_window_k=4,
        max_tokens=512,
        tools=tools,
        safety_margin_tokens=128,
        threshold_percent=75,
    )

    expected_system_tokens = estimate_tokens(message_token_text(messages[0]))
    expected_non_system_tokens = sum(estimate_tokens(message_token_text(message)) for message in messages[1:])
    expected_tools_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False))
    expected_input_limit = 4 * CONTEXT_WINDOW_TOKENS_PER_K - 512 - 128

    assert usage.system_tokens == expected_system_tokens
    assert usage.non_system_tokens == expected_non_system_tokens
    assert usage.budget.tools_tokens == expected_tools_tokens
    assert usage.budget.context_window_tokens == 4000
    assert usage.required_input_tokens == expected_system_tokens + expected_non_system_tokens + expected_tools_tokens
    assert usage.summary_trigger_tokens == expected_input_limit * 75 // 100
    assert usage.exceeds_hard_window == (usage.required_input_tokens > expected_input_limit)


def _build_over_window_assistant_request():
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="stable system prompt"),
        InternalMessage(id=1, role=MessageRole.USER, content="historical context " * 2000),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="final assistant response"),
    ]
    usage = measure_context_request_usage(
        messages=messages,
        context_window_k=1,
        max_tokens=0,
        tools=None,
        safety_margin_tokens=0,
    )
    hard_input_limit = usage.budget.context_window_tokens - usage.budget.output_tokens - usage.budget.safety_margin_tokens
    return messages, usage, hard_input_limit


def test_final_request_accepts_hard_window_input_override_without_changing_messages():
    messages, usage, hard_input_limit = _build_over_window_assistant_request()

    assert usage.exceeds_hard_window
    assert usage.required_input_tokens > hard_input_limit
    original_contents = [message.content for message in messages]

    request_messages = ContextManager.trim_messages_for_model_request(
        messages=messages,
        uid="user-1",
        session_id="session-1",
        context_window_k=1,
        max_tokens=0,
        tools=None,
        safety_margin_tokens=0,
        required_input_tokens_override=hard_input_limit,
    )

    assert [message.content for message in request_messages] == original_contents
    assert [message.content for message in messages] == original_contents


def test_final_request_rejects_input_override_above_hard_window_with_context_budget_error():
    messages, usage, hard_input_limit = _build_over_window_assistant_request()

    assert usage.exceeds_hard_window
    with pytest.raises(ParameterException) as exc_info:
        ContextManager.trim_messages_for_model_request(
            messages=messages,
            uid="user-1",
            session_id="session-1",
            context_window_k=1,
            max_tokens=0,
            tools=None,
            safety_margin_tokens=0,
            required_input_tokens_override=hard_input_limit + 1,
        )

    assert exc_info.value.message == ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED


@pytest.mark.parametrize("invalid_override", [True, -1], ids=["boolean_true", "negative"])
def test_final_request_invalid_input_override_falls_back_to_local_full_estimate(invalid_override):
    messages, usage, _ = _build_over_window_assistant_request()

    assert usage.exceeds_hard_window
    with pytest.raises(ParameterException) as exc_info:
        ContextManager.trim_messages_for_model_request(
            messages=messages,
            uid="user-1",
            session_id="session-1",
            context_window_k=1,
            max_tokens=0,
            tools=None,
            safety_margin_tokens=0,
            required_input_tokens_override=invalid_override,
        )

    assert exc_info.value.message == ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED


def test_final_request_hard_window_check_rejects_without_sliding_history():
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="stable system prompt"),
        InternalMessage(id=1, role=MessageRole.USER, content="discardable history " * 300),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="discardable answer " * 300),
        InternalMessage(id=3, role=MessageRole.USER, content="protected earlier question"),
        InternalMessage(id=4, role=MessageRole.ASSISTANT, content="protected earlier answer"),
        InternalMessage(id=5, role=MessageRole.USER, content="protected latest question"),
        InternalMessage(id=6, role=MessageRole.ASSISTANT, content="protected latest answer"),
        InternalMessage(id=7, role=MessageRole.USER, content="current request"),
    ]
    tools = [{"type": "function", "function": {"name": "lookup", "description": "lookup tool"}}]

    original_contents = [message.content for message in messages]
    with pytest.raises(ParameterException):
        ContextManager.trim_messages_for_model_request(
            messages=messages,
            uid="user-1",
            session_id="session-1",
            context_window_k=1,
            max_tokens=256,
            tools=tools,
            safety_margin_tokens=64,
        )

    assert [message.content for message in messages] == original_contents


def test_summary_threshold_and_hard_window_share_one_required_input_value():
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system policy " * 10),
        InternalMessage(role=MessageRole.USER, content="current input " * 20),
    ]
    kwargs = {
        "messages": messages,
        "context_window_k": 1,
        "max_tokens": 128,
        "tools": [{"type": "function", "function": {"name": "shell"}}],
        "safety_margin_tokens": 64,
    }

    threshold_usage = measure_context_request_usage(
        **kwargs,
        threshold_percent=50,
    )
    hard_window_usage = measure_context_request_usage(
        **kwargs,
        threshold_percent=100,
    )

    assert threshold_usage.required_input_tokens == hard_window_usage.required_input_tokens
    assert threshold_usage.budget == hard_window_usage.budget
    assert threshold_usage.summary_trigger_tokens * 2 <= hard_window_usage.summary_trigger_tokens + 1


def test_final_request_budget_reserves_runtime_non_system_tokens():
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        InternalMessage(role=MessageRole.USER, content="current request"),
    ]

    ContextManager.trim_messages_for_model_request(
        messages=messages,
        uid="user-1",
        session_id="session-1",
        context_window_k=1,
        max_tokens=0,
        tools=None,
        safety_margin_tokens=0,
    )

    with pytest.raises(ParameterException):
        ContextManager.trim_messages_for_model_request(
            messages=messages,
            uid="user-1",
            session_id="session-1",
            context_window_k=1,
            max_tokens=0,
            tools=None,
            safety_margin_tokens=0,
            additional_non_system_tokens=CONTEXT_WINDOW_TOKENS_PER_K,
        )


def test_final_request_budget_preserves_longterm_memory_recall_json():
    recall_payload = json.dumps(
        {
            "items": [
                {
                    "memory_id": 1,
                    "expected_version": 2,
                    "memory_key": "project",
                    "memory_type": "project",
                    "content": "memory content " * 220,
                }
            ],
            "knowledge_base": [
                {
                    "knowledge_base_id": 10,
                    "knowledge_base_name": "Docs",
                    "source_type": "user_knowledge",
                    "source": "guide.md",
                    "content": "knowledge content " * 180,
                    "truncated": False,
                    "llm_maintainable": False,
                }
            ],
            "chat_history": [
                {
                    "role": "user",
                    "content": "historical content " * 160,
                    "session_id": "older-session",
                    "created_at": "2026-09-01 10:00:00",
                }
            ],
            "current_session_id": "session-1",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system prompt"),
        InternalMessage(id=1, role=MessageRole.USER, content="current question"),
        InternalMessage(
            id=2,
            role=MessageRole.ASSISTANT,
            tool_calls=[
                InternalToolCall(
                    id="memory-recall-1",
                    name="manage_memory_and_knowledge",
                    arguments={
                        "operation": "recall",
                        "query": "stable background",
                        "knowledge_query": "document question",
                    },
                )
            ],
        ),
        InternalMessage(
            id=3,
            role=MessageRole.TOOL,
            tool_call_id="memory-recall-1",
            content=recall_payload,
        ),
    ]

    request_messages = ContextManager.trim_messages_for_model_request(
        messages=messages,
        uid="user-1",
        session_id="session-1",
        context_window_k=20,
        max_tokens=256,
        tools=None,
        safety_margin_tokens=64,
    )

    tool_message = next(message for message in request_messages if message.role == MessageRole.TOOL)
    assert tool_message.content == recall_payload
