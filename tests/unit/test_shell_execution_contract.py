import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.terminal import (
    ShellExecutionMode,
    ShellExecutionResult,
    ShellInteractiveHandoffResult,
    ShellNonInteractiveCompletedResult,
    ShellNonInteractiveTimeoutResult,
    TerminalOutputBufferState,
    TerminalSessionStatus,
    validate_shell_execution_mode,
)

TERMINAL_SESSION_ID = "t" * 32
SHELL_EXECUTION_RESULT_ADAPTER = TypeAdapter(ShellExecutionResult)


def _output_buffer() -> TerminalOutputBufferState:
    return TerminalOutputBufferState(
        capacity_bytes=1024,
        oldest_offset=0,
        next_offset=5,
        oldest_sequence=1,
        next_sequence=2,
    )


def _handoff_payload(**overrides):
    payload = {
        "terminal_session_id": TERMINAL_SESSION_ID,
        "status": TerminalSessionStatus.RUNNING,
        "output_buffer": _output_buffer(),
    }
    payload.update(overrides)
    return payload


def test_shell_execution_mode_accepts_only_exact_values():
    assert [mode.value for mode in ShellExecutionMode] == ["interactive", "non_interactive"]
    assert validate_shell_execution_mode("interactive") is ShellExecutionMode.INTERACTIVE
    assert validate_shell_execution_mode("non_interactive") is ShellExecutionMode.NON_INTERACTIVE


@pytest.mark.parametrize("value", ["auto", "AUTO", "Interactive", "NON_INTERACTIVE", None, 1, True, {}])
def test_validate_shell_execution_mode_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        validate_shell_execution_mode(value)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ShellNonInteractiveCompletedResult,
            {"stdout": "out", "stderr": "err", "exit_code": 0, "system_info": "system"},
        ),
        (
            ShellNonInteractiveTimeoutResult,
            {"error": "timed out", "system_info": "system"},
        ),
    ],
)
def test_non_interactive_result_models_validate_fields_and_forbid_extra_fields(model, payload):
    result = model.model_validate(payload)

    assert result.model_dump() == payload
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "extra": "not allowed"})


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (TerminalSessionStatus.RUNNING, {}),
        (TerminalSessionStatus.EXITED, {"exit_code": 0}),
        (TerminalSessionStatus.FAILED, {"failure_reason": "driver error"}),
    ],
)
def test_interactive_handoff_result_validates_active_and_terminal_outcomes(status, outcome):
    result = ShellInteractiveHandoffResult(
        **_handoff_payload(status=status, **outcome),
    )

    assert result.output_stream == "merged_stdout_stderr"
    assert result.output_buffer == _output_buffer()


def test_interactive_handoff_result_requires_merged_output_stream():
    with pytest.raises(ValidationError):
        ShellInteractiveHandoffResult(**_handoff_payload(output_stream="stdout"))


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (TerminalSessionStatus.RUNNING, {"exit_code": 0}),
        (TerminalSessionStatus.RUNNING, {"failure_reason": "still running"}),
        (TerminalSessionStatus.EXITED, {}),
        (TerminalSessionStatus.EXITED, {"failure_reason": "driver error"}),
        (TerminalSessionStatus.FAILED, {}),
        (TerminalSessionStatus.FAILED, {"exit_code": 1}),
    ],
)
def test_interactive_handoff_result_rejects_invalid_status_outcomes(status, outcome):
    with pytest.raises(ValidationError):
        ShellInteractiveHandoffResult(**_handoff_payload(status=status, **outcome))


@pytest.mark.parametrize(
    ("payload", "expected_type"),
    [
        (
            {"stdout": "out", "stderr": "", "exit_code": 0, "system_info": "system"},
            ShellNonInteractiveCompletedResult,
        ),
        (
            {"error": "timed out", "system_info": "system"},
            ShellNonInteractiveTimeoutResult,
        ),
        (
            _handoff_payload(status=TerminalSessionStatus.EXITED, exit_code=0),
            ShellInteractiveHandoffResult,
        ),
    ],
)
def test_shell_execution_result_adapter_recognizes_all_contract_variants(payload, expected_type):
    result = SHELL_EXECUTION_RESULT_ADAPTER.validate_python(payload)

    assert isinstance(result, expected_type)
