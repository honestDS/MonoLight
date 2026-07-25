import json

from app.core import log as log_module
from app.core.utils.background_task_result import sanitize_execution_summary, sanitize_execution_value
from app.core.utils.dispatcher import helpers


class CapturingLogger:
    def __init__(self):
        self.bindings = []
        self.messages = []

    def bind(self, **kwargs):
        self.bindings.append(kwargs)
        return self

    def info(self, message):
        self.messages.append(message)

    def opt(self, *, exception):
        return self

    def error(self, message):
        self.messages.append(message)


def test_execution_summary_redacts_nested_secrets_and_shell_output():
    payload = {
        "status": "failed",
        "stdout": "SHELL_OUTPUT_SHOULD_NOT_BE_PERSISTED",
        "stderr": "STDERR_OUTPUT_SHOULD_NOT_BE_PERSISTED",
        "nested": {
            "api_key": "API_SECRET_VALUE",
            "items": [{"password": "PASSWORD_SECRET_VALUE"}],
            "error": "token=TOKEN_SECRET_VALUE",
        },
    }

    summary = sanitize_execution_summary(payload)

    assert "SHELL_OUTPUT_SHOULD_NOT_BE_PERSISTED" not in summary
    assert "STDERR_OUTPUT_SHOULD_NOT_BE_PERSISTED" not in summary
    assert "API_SECRET_VALUE" not in summary
    assert "PASSWORD_SECRET_VALUE" not in summary
    assert "TOKEN_SECRET_VALUE" not in summary
    assert '"status": "failed"' in summary
    assert sanitize_execution_summary("RAW_PLAIN_SHELL_OUTPUT", redact_text=True) != "RAW_PLAIN_SHELL_OUTPUT"


def test_tool_result_conversion_keeps_non_sensitive_output_for_model_result():
    payload = {"stdout": "合法工具输出", "nested": {"api_key": "SECRET_VALUE"}}

    result = sanitize_execution_value(payload)

    assert result["stdout"] == "合法工具输出"
    assert result["nested"]["api_key"] == "<redacted>"


def test_tool_result_log_keeps_json_output_and_nested_error(monkeypatch):
    logger = CapturingLogger()
    monkeypatch.setattr(log_module, "logger", logger)

    log_module.LogManager.log_tool_result(
        1,
        json.dumps(
            {
                "stdout": "RAW_SHELL_OUTPUT",
                "error": "request failed: password=RAW_PASSWORD",
                "details": {"authorization": "Bearer RAW_TOKEN"},
            },
            ensure_ascii=False,
        ),
        "session-1",
        "user-1",
    )

    assert len(logger.messages) == 1
    assert "RAW_SHELL_OUTPUT" in logger.messages[0]
    assert "RAW_PASSWORD" in logger.messages[0]
    assert "RAW_TOKEN" in logger.messages[0]


def test_exception_log_uses_the_same_safe_summary(monkeypatch):
    logger = CapturingLogger()
    monkeypatch.setattr(helpers, "logger", logger)

    helpers.format_exception_message(RuntimeError("shell failed: secret=RAW_SECRET password=RAW_PASSWORD"))

    assert logger.bindings
    exception_message = logger.bindings[0]["exception_message"]
    assert "RAW_SECRET" not in exception_message
    assert "RAW_PASSWORD" not in exception_message
