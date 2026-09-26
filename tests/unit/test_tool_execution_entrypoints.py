from types import SimpleNamespace

from app.core.session_reply_queue import executor_confirmed as executor_confirmed_module
from app.core.tools import TOOL_EXECUTOR_MAP, tool_requires_audit
from app.models.message import InternalMessage, InternalToolCall, MessageRole


def test_registered_tools_explicitly_declare_audit_requirement():
    assert all(isinstance(executor_class.requires_audit, bool) for executor_class in TOOL_EXECUTOR_MAP.values())
    audited_tools = {"execute_shell", "write_file", "terminal_write", "terminal_close"}
    assert {tool_name for tool_name in TOOL_EXECUTOR_MAP if tool_requires_audit(tool_name)} == audited_tools
    assert "read_multimodal_file" in TOOL_EXECUTOR_MAP
    assert tool_requires_audit("read_multimodal_file") is False
    assert all(not tool_requires_audit(tool_name) for tool_name in TOOL_EXECUTOR_MAP if tool_name not in audited_tools)
    assert tool_requires_audit("unknown_tool") is False


def test_confirmed_tool_result_budget_reuses_last_model_context_window():
    session = SimpleNamespace(llm_request_metadata={"context_window_tokens": 1_050_000})

    assert executor_confirmed_module._resolve_confirmed_tool_context_window_k(session) == 1050
    assert executor_confirmed_module._resolve_confirmed_tool_context_window_k(SimpleNamespace(llm_request_metadata=None)) == 4


def test_confirmed_tool_result_budget_uses_provider_input_plus_confirmed_tool_call(monkeypatch):
    session = SimpleNamespace(
        llm_request_metadata={
            "context_window_tokens": 250_000,
            "max_output_tokens": 20_480,
            "input_tokens": 185_765,
            "input_tokens_source": "provider",
        }
    )
    confirmed_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(
                id="call-confirmed",
                name="execute_shell",
                arguments={"command": "git diff", "execution_mode": "non_interactive"},
            )
        ],
    )
    monkeypatch.setattr(executor_confirmed_module, "estimate_tokens", lambda _text: 5_000)
    monkeypatch.setattr(executor_confirmed_module, "message_token_text", lambda _message: "confirmed-tool-call")

    budget_tokens = executor_confirmed_module._resolve_confirmed_tool_result_round_budget_tokens(
        session,
        confirmed_message,
        tools=[],
    )

    assert budget_tokens == (250_000 - 20_480 - 256 - 190_765) // 2
