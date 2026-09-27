import json
from types import SimpleNamespace

import pytest

from app.core.constants import TOOL_RESULT_COMPACT_TRUNCATION_NOTICE, TOOL_RESULT_MINIMAL_TRUNCATION_NOTICE
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


def test_tool_result_round_budget_falls_back_to_local_usage_when_provider_baseline_already_exceeds_window(monkeypatch):
    usage = SimpleNamespace(
        budget=SimpleNamespace(
            context_window_tokens=250_000,
            output_tokens=20_480,
            safety_margin_tokens=256,
        ),
        required_input_tokens=175_849,
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
        required_input_tokens_override=352_375,
        fallback_to_local_usage_on_overflow=True,
    )

    assert budget_tokens == (250_000 - 20_480 - 256 - 175_849) // 2


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


def test_untruncated_json_keeps_original_whitespace_and_real_token_stats():
    content = (
        json.dumps(
            {"status": "ready", "items": ["first", "second"]},
            ensure_ascii=False,
            indent=4,
        )
        + "\n"
    )
    encoding = truncate_tool_result_module.tiktoken.get_encoding("cl100k_base")
    original_tokens = len(encoding.encode(content, disallowed_special=()))
    compact_tokens = len(
        encoding.encode(
            json.dumps(json.loads(content), ensure_ascii=False, separators=(",", ":")),
            disallowed_special=(),
        )
    )

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=original_tokens,
    )

    assert compact_tokens < original_tokens
    assert result.content == content
    assert result.truncated is False
    assert result.original_tokens == original_tokens
    assert result.final_tokens == original_tokens


def test_overlimit_json_preserves_prefix_fields_and_truncates_string_inside_json(monkeypatch):
    notice = "[TRUNCATED]"
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: notice)

    payload = {
        "status": "ready",
        "request_id": "req-123",
        "details": "prefix-" + "segment-" * 400,
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoding = truncate_tool_result_module.tiktoken.get_encoding("cl100k_base")
    original_tokens = len(encoding.encode(content, disallowed_special=()))
    fitting_prefix = "prefix-" + "segment-" * 8
    fitting_content = json.dumps(
        {
            "status": "ready",
            "request_id": "req-123",
            "details": fitting_prefix + notice,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    limit_tokens = len(encoding.encode(fitting_content, disallowed_special=()))

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=limit_tokens,
    )
    truncated_payload = json.loads(result.content)

    assert original_tokens > limit_tokens
    assert result.truncated is True
    assert result.original_tokens == original_tokens
    assert result.final_tokens == len(encoding.encode(result.content, disallowed_special=()))
    assert result.final_tokens <= limit_tokens
    assert truncated_payload["status"] == "ready"
    assert truncated_payload["request_id"] == "req-123"
    assert truncated_payload["details"].startswith(fitting_prefix)
    assert truncated_payload["details"].endswith(notice)
    assert len(truncated_payload["details"]) < len(payload["details"])


def test_overlimit_json_keeps_later_object_fields_when_earlier_value_is_large(monkeypatch):
    notice = "[TRUNCATED]"
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: notice)

    payload = {
        "stdout": "output-" * 400,
        "stderr": "fatal warning",
        "exit_code": 7,
        "system_info": "windows",
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoding = truncate_tool_result_module.tiktoken.get_encoding("cl100k_base")
    expected_tail = json.dumps(
        {
            "stdout": notice,
            "stderr": payload["stderr"],
            "exit_code": payload["exit_code"],
            "system_info": payload["system_info"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    limit_tokens = len(encoding.encode(expected_tail, disallowed_special=())) + 20

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=limit_tokens,
    )
    truncated_payload = json.loads(result.content)

    assert result.truncated is True
    assert list(truncated_payload) == list(payload)
    assert truncated_payload["stdout"].endswith(notice)
    assert truncated_payload["stderr"] == payload["stderr"]
    assert truncated_payload["exit_code"] == payload["exit_code"]
    assert truncated_payload["system_info"] == payload["system_info"]
    assert result.final_tokens <= limit_tokens


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "x" * 200},
        ["x" * 200],
        "x" * 200,
        10**100,
    ],
)
def test_tiny_json_budget_returns_parseable_json_for_each_value_shape(payload):
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoding = truncate_tool_result_module.tiktoken.get_encoding("cl100k_base")

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=1,
    )

    json.loads(result.content)
    assert result.truncated is True
    assert result.final_tokens == len(encoding.encode(result.content, disallowed_special=()))
    assert result.final_tokens <= 1


def test_json_truncation_fallback_uses_character_estimates_and_stays_parseable(monkeypatch):
    notice = "[TRUNCATED]"
    monkeypatch.setattr(
        truncate_tool_result_module.tiktoken,
        "get_encoding",
        lambda _name: (_ for _ in ()).throw(RuntimeError("tokenizer unavailable")),
    )
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: notice)

    payload = {
        "status": "ready",
        "message": "前缀" + "中文内容" * 300,
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    original_tokens = truncate_tool_result_module._estimate_tokens_by_chars(content)
    fitting_content = json.dumps(
        {"status": "ready", "message": "前缀" + notice},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    limit_tokens = max(1, truncate_tool_result_module._estimate_tokens_by_chars(fitting_content))

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=limit_tokens,
    )
    truncated_payload = json.loads(result.content)

    assert original_tokens > limit_tokens
    assert result.truncated is True
    assert truncated_payload["status"] == "ready"
    assert notice in truncated_payload["message"]
    assert result.original_tokens == original_tokens
    assert result.final_tokens == truncate_tool_result_module._estimate_tokens_by_chars(result.content)
    assert result.final_tokens <= limit_tokens


def test_character_estimate_fallback_keeps_large_chinese_non_json_within_budget(monkeypatch):
    notice = "[TRUNCATED]"
    monkeypatch.setattr(
        truncate_tool_result_module.tiktoken,
        "get_encoding",
        lambda _name: (_ for _ in ()).throw(RuntimeError("tokenizer unavailable")),
    )
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: notice)

    content = "这是大量中文内容，用于验证字符估算回退。" * 300
    original_tokens = truncate_tool_result_module._estimate_tokens_by_chars(content)
    limit_tokens = max(
        truncate_tool_result_module._estimate_tokens_by_chars(notice) + 1,
        original_tokens // 4,
    )

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        content,
        context_window_k=1,
        limit_tokens=limit_tokens,
    )

    assert original_tokens > limit_tokens
    assert result.truncated is True
    assert result.original_tokens == original_tokens
    assert result.final_tokens == truncate_tool_result_module._estimate_tokens_by_chars(result.content)
    assert result.final_tokens <= limit_tokens
    assert notice in result.content
    assert len(result.content) < len(content)


def test_truncated_tool_result_uses_budget_safe_notice_when_full_notice_does_not_fit(monkeypatch):
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: "[TRUNCATED]")

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        "x" * 100,
        context_window_k=1,
        limit_tokens=1,
    )

    assert result.truncated is True
    assert result.content == TOOL_RESULT_MINIMAL_TRUNCATION_NOTICE
    assert result.final_tokens <= 1


def test_truncated_tool_result_fallback_uses_budget_safe_notice_when_full_notice_does_not_fit(monkeypatch):
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
    assert result.content == TOOL_RESULT_MINIMAL_TRUNCATION_NOTICE
    assert result.final_tokens <= 1


def test_parallel_tiny_tool_budgets_do_not_expand_from_truncation_notices():
    messages = [InternalMessage(role=MessageRole.TOOL, tool_call_id=f"call-{index}", content="x" * 100) for index in range(20)]

    truncate_tool_result_module.truncate_tool_messages_for_budget(
        messages,
        context_window_k=1,
        budget_tokens=20,
        uid="user-1",
        session_id="session-1",
    )

    assert all(message.content == TOOL_RESULT_MINIMAL_TRUNCATION_NOTICE for message in messages)


def test_compact_truncation_notice_is_shorter_than_full_notice(monkeypatch):
    monkeypatch.setattr(truncate_tool_result_module, "_get_truncation_notice", lambda: "[TRUNCATED]")

    result = truncate_tool_result_module.truncate_tool_result_with_stats(
        "x" * 100,
        context_window_k=1,
        limit_tokens=2,
    )

    assert result.content == TOOL_RESULT_COMPACT_TRUNCATION_NOTICE


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
