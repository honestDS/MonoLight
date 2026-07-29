import json

from app.core import log as log_module
from app.core.utils.background_task_result import normalize_execution_value, serialize_execution_summary
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


def test_execution_summary_keeps_nested_values_and_output():
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

    summary = serialize_execution_summary(payload)

    assert "SHELL_OUTPUT_SHOULD_NOT_BE_PERSISTED" in summary
    assert "STDERR_OUTPUT_SHOULD_NOT_BE_PERSISTED" in summary
    assert "API_SECRET_VALUE" in summary
    assert "PASSWORD_SECRET_VALUE" in summary
    assert "TOKEN_SECRET_VALUE" in summary
    assert '"status": "failed"' in summary
    assert serialize_execution_summary("RAW_PLAIN_SHELL_OUTPUT") == "RAW_PLAIN_SHELL_OUTPUT"


def test_execution_value_normalization_keeps_all_values():
    payload = {"stdout": "合法工具输出", "nested": {"api_key": "SECRET_VALUE"}}

    result = normalize_execution_value(payload)

    assert result["stdout"] == "合法工具输出"
    assert result["nested"]["api_key"] == "SECRET_VALUE"


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


def test_exception_log_keeps_original_exception_message(monkeypatch):
    logger = CapturingLogger()
    monkeypatch.setattr(helpers, "logger", logger)

    helpers.format_exception_message(RuntimeError("shell failed: secret=RAW_SECRET password=RAW_PASSWORD"))

    assert logger.bindings
    exception_message = logger.bindings[0]["exception_message"]
    assert "RAW_SECRET" in exception_message
    assert "RAW_PASSWORD" in exception_message
