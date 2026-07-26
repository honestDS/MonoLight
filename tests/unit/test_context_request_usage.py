import json

from app.core.constants import CONTEXT_WINDOW_TOKENS_PER_K
from app.core.context import ContextManager
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


def test_final_request_hard_window_check_uses_same_complete_request_measurement():
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

    request_messages = ContextManager.trim_messages_for_model_request(
        messages=messages,
        uid="user-1",
        session_id="session-1",
        context_window_k=1,
        max_tokens=256,
        tools=tools,
        safety_margin_tokens=64,
    )
    usage = measure_context_request_usage(
        messages=request_messages,
        context_window_k=1,
        max_tokens=256,
        tools=tools,
        safety_margin_tokens=64,
    )

    assert request_messages[-1].id == 7
    assert not usage.exceeds_hard_window


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
