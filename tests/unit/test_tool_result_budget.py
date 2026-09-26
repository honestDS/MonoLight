from types import SimpleNamespace

from app.core.dispatchers import interactive_tools as interactive_tools_module
from app.core.utils.dispatcher import truncate_tool_result as truncate_tool_result_module
from app.models.message import InternalMessage, InternalToolCall, MessageRole


def test_tool_result_round_budget_uses_half_of_remaining_request_input(monkeypatch):
    usage = SimpleNamespace(
        budget=SimpleNamespace(
            context_window_tokens=250_000,
            output_tokens=20_480,
            safety_margin_tokens=256,
        ),
        required_input_tokens=185_765,
    )
    monkeypatch.setattr(
        truncate_tool_result_module,
        "measure_context_request_usage",
        lambda **_kwargs: usage,
    )

    budget_tokens = truncate_tool_result_module.calculate_tool_result_round_budget_tokens(
        messages=[],
        context_window_k=250,
        max_tokens=20_480,
        tools=[],
    )

    assert budget_tokens == (250_000 - 20_480 - 256 - 185_765) // 2


def test_tool_result_round_budget_shrinks_as_current_request_grows(monkeypatch):
    usages = iter(
        [
            SimpleNamespace(
                budget=SimpleNamespace(
                    context_window_tokens=250_000,
                    output_tokens=20_480,
                    safety_margin_tokens=256,
                ),
                required_input_tokens=100_000,
            ),
            SimpleNamespace(
                budget=SimpleNamespace(
                    context_window_tokens=250_000,
                    output_tokens=20_480,
                    safety_margin_tokens=256,
                ),
                required_input_tokens=180_000,
            ),
        ]
    )
    monkeypatch.setattr(
        truncate_tool_result_module,
        "measure_context_request_usage",
        lambda **_kwargs: next(usages),
    )

    first_budget = truncate_tool_result_module.calculate_tool_result_round_budget_tokens(
        messages=[],
        context_window_k=250,
        max_tokens=20_480,
        tools=[],
    )
    second_budget = truncate_tool_result_module.calculate_tool_result_round_budget_tokens(
        messages=[],
        context_window_k=250,
        max_tokens=20_480,
        tools=[],
    )

    assert first_budget == 64_632
    assert second_budget == 24_632
    assert second_budget < first_budget


def test_tool_result_round_budget_prefers_explicit_required_input_tokens(monkeypatch):
    monkeypatch.setattr(
        truncate_tool_result_module,
        "measure_context_request_usage",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("local history estimate must not be used")),
    )

    budget_tokens = truncate_tool_result_module.calculate_tool_result_round_budget_tokens(
        messages=[],
        context_window_k=250,
        max_tokens=20_480,
        tools=[],
        required_input_tokens_override=190_000,
    )

    assert budget_tokens == (250_000 - 20_480 - 256 - 190_000) // 2


def test_interactive_tool_budget_extends_provider_input_by_current_tool_call(monkeypatch):
    state = SimpleNamespace(
        latest_llm_request_metadata={
            "input_tokens": 185_765,
            "input_tokens_source": "provider",
        }
    )
    ai_msg = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(
                id="call-1",
                name="execute_shell",
                arguments={"command": "git diff", "execution_mode": "non_interactive"},
            )
        ],
    )
    monkeypatch.setattr(interactive_tools_module, "message_token_text", lambda _message: "tool-call")
    monkeypatch.setattr(interactive_tools_module, "estimate_tokens", lambda _text: 5_000)

    required_input_tokens = interactive_tools_module._resolve_tool_result_required_input_tokens(
        state,
        ai_msg,
    )

    assert required_input_tokens == 190_765


def test_interactive_tool_budget_uses_local_fallback_without_provider_usage():
    state = SimpleNamespace(
        latest_llm_request_metadata={
            "input_tokens": 185_765,
            "input_tokens_source": "estimated",
        }
    )
    ai_msg = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(
                id="call-1",
                name="execute_shell",
                arguments={"command": "git diff", "execution_mode": "non_interactive"},
            )
        ],
    )

    assert interactive_tools_module._resolve_tool_result_required_input_tokens(state, ai_msg) is None


def test_truncated_tool_result_keeps_notice_when_budget_is_smaller_than_notice(monkeypatch):
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: "[TRUNCATED]")

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        "x" * 100,
        context_window_k=1,
        limit_tokens=1,
    )

    assert result.truncated is True
    assert result.content == "[TRUNCATED]"


def test_truncated_tool_result_fallback_keeps_notice_when_budget_is_smaller_than_notice(monkeypatch):
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: "[TRUNCATED]")
    monkeypatch.setattr(
        truncate_tool_result_module.tiktoken,
        "get_encoding",
        lambda _name: (_ for _ in ()).throw(RuntimeError("tokenizer unavailable")),
    )

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        "x" * 100,
        context_window_k=1,
        limit_tokens=1,
    )

    assert result.truncated is True
    assert result.content == "[TRUNCATED]"


def test_tool_result_round_budget_never_becomes_non_positive(monkeypatch):
    usage = SimpleNamespace(
        budget=SimpleNamespace(
            context_window_tokens=8_000,
            output_tokens=2_000,
            safety_margin_tokens=256,
        ),
        required_input_tokens=6_000,
    )
    monkeypatch.setattr(
        truncate_tool_result_module,
        "measure_context_request_usage",
        lambda **_kwargs: usage,
    )

    budget_tokens = truncate_tool_result_module.calculate_tool_result_round_budget_tokens(
        messages=[],
        context_window_k=8,
        max_tokens=2_000,
        tools=[],
    )

    assert budget_tokens == 1
