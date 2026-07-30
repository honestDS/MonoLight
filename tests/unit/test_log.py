from types import SimpleNamespace

from app.core.log import LogManager


def test_format_db_log_message_appends_exception_traceback():
    try:
        raise RuntimeError("database unavailable")
    except RuntimeError as error:
        exception = SimpleNamespace(type=type(error), value=error, traceback=error.__traceback__)

    message = LogManager._format_db_log_message("Exception in callback handler", exception)

    assert message.startswith("Exception in callback handler\nTraceback (most recent call last):")
    assert message.endswith("RuntimeError: database unavailable\n")


def test_format_db_log_message_keeps_message_without_exception():
    message = LogManager._format_db_log_message("Normal log message", None)

    assert message == "Normal log message"
