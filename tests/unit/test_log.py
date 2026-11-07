import asyncio
import json
from types import SimpleNamespace

import pytest
from loguru import logger as loguru_logger

from app.core.log import LogManager, agent_log_context, get_logger
from app.core.log.redaction import redact_log_text


def _redact_and_assert_removed(text, *sensitive_values):
    redacted = redact_log_text(text)

    for value in sensitive_values:
        assert value not in redacted
    assert redact_log_text(redacted) == redacted

    return redacted


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


async def test_agent_log_context_patches_call_time_context_across_concurrent_tasks():
    records = []
    sink_id = loguru_logger.add(
        lambda message: records.append({"message": message.record["message"], "extra": dict(message.record["extra"])}),
        level="DEBUG",
    )
    logger = get_logger("tests.unit.test_log.agent_context")

    main_started = asyncio.Event()
    subagent_review_started = asyncio.Event()
    main_continued = asyncio.Event()
    subagent_review_continued = asyncio.Event()
    main_resumed = asyncio.Event()
    subagent_analysis_finished = asyncio.Event()

    def emit(event: str) -> None:
        logger.bind(event=event).info(event)

    async def main_agent() -> None:
        with agent_log_context():
            emit("主代理开始")
            main_started.set()
            await subagent_review_started.wait()
            emit("主代理继续")
            main_continued.set()
            await subagent_review_continued.wait()
            emit("主代理恢复")
            main_resumed.set()
            await subagent_analysis_finished.wait()
            emit("主代理结束")

    async def review_subagent() -> None:
        with agent_log_context("代码审阅{员}"):
            await main_started.wait()
            emit("代码审阅开始")
            subagent_review_started.set()
            await main_continued.wait()
            emit("代码审阅继续")
            subagent_review_continued.set()

    async def analysis_subagent() -> None:
        with agent_log_context("测试分析员"):
            await subagent_review_continued.wait()
            emit("测试分析开始")
            await main_resumed.wait()
            emit("测试分析结束")
            subagent_analysis_finished.set()

    try:
        await asyncio.gather(main_agent(), review_subagent(), analysis_subagent())
        emit("正常退出后")

        try:
            with agent_log_context("异常恢复{员}"):
                emit("异常上下文内")
                raise RuntimeError("leave agent context")
        except RuntimeError:
            pass

        emit("异常恢复后")
    finally:
        loguru_logger.remove(sink_id)

    records_by_event = {record["extra"]["event"]: record for record in records}
    expected_contexts = {
        "主代理开始": ("[主代理]", "main", None),
        "主代理继续": ("[主代理]", "main", None),
        "主代理恢复": ("[主代理]", "main", None),
        "主代理结束": ("[主代理]", "main", None),
        "代码审阅开始": ("[子代理：代码审阅{员}]", "subagent", "代码审阅{员}"),
        "代码审阅继续": ("[子代理：代码审阅{员}]", "subagent", "代码审阅{员}"),
        "测试分析开始": ("[子代理：测试分析员]", "subagent", "测试分析员"),
        "测试分析结束": ("[子代理：测试分析员]", "subagent", "测试分析员"),
        "异常上下文内": ("[子代理：异常恢复{员}]", "subagent", "异常恢复{员}"),
    }
    assert set(records_by_event) == set(expected_contexts) | {"正常退出后", "异常恢复后"}

    for event, (prefix, role, subagent_name) in expected_contexts.items():
        record = records_by_event[event]
        assert record["message"].startswith(f"{prefix} ")
        assert record["extra"]["agent_role"] == role
        assert record["extra"]["subagent_name"] == subagent_name

    for event in ("正常退出后", "异常恢复后"):
        record = records_by_event[event]
        assert record["message"] == event
        assert not {"agent_label", "agent_role", "subagent_name"} & record["extra"].keys()


def test_tool_logs_keep_agent_prefix_and_tool_markers():
    records = []
    sink_id = loguru_logger.add(
        lambda message: records.append({"message": message.record["message"], "extra": dict(message.record["extra"])}),
        level="DEBUG",
    )

    try:
        with agent_log_context("工具操作员"):
            LogManager.log_tool_call(turn=1, tool_name="search", command="query", session_id="session")
            LogManager.log_tool_result(turn=1, result="found", session_id="session")
    finally:
        loguru_logger.remove(sink_id)

    assert len(records) == 2
    call_record = next(record for record in records if record["extra"].get("tool_call"))
    result_record = next(record for record in records if record["extra"].get("tool_result"))

    for record in (call_record, result_record):
        assert record["message"].startswith("[子代理：工具操作员] ")
        assert record["extra"]["agent_role"] == "subagent"
        assert record["extra"]["subagent_name"] == "工具操作员"

    assert call_record["extra"]["tool_call"] is True
    assert "tool_result" not in call_record["extra"]
    assert result_record["extra"]["tool_result"] is True
    assert "tool_call" not in result_record["extra"]


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("password", "lowercase-password-secret"),
        ("PASSWORD", "uppercase-password-secret"),
        ("api-key", "hyphenated-api-key-secret"),
        ("apiKey", "camel-case-api-key-secret"),
        ("OPENAI_API_KEY", "openai-api-key-secret"),
        ("hashed_password", "hashed-password-secret"),
        ("botToken", "bot-token-secret"),
    ],
)
def test_redact_log_text_masks_sensitive_json_keys_and_preserves_metadata(key, secret):
    text = json.dumps(
        {
            key: secret,
            "max_tokens": 256,
            "token_count": 13,
            "uid": "uid-42",
            "session_id": "session-73",
        }
    )

    redacted = _redact_and_assert_removed(text, secret)
    payload = json.loads(redacted)

    assert payload[key] != secret
    assert payload["max_tokens"] == 256
    assert payload["token_count"] == 13
    assert payload["uid"] == "uid-42"
    assert payload["session_id"] == "session-73"


def test_redact_log_text_masks_nested_json_objects_and_lists():
    secrets = [
        "nested-password-secret",
        "list-api-key-secret",
        "whole-credentials-value-secret",
    ]
    text = json.dumps(
        {
            "max_tokens": 512,
            "token_count": 27,
            "uid": "nested-uid",
            "session_id": "nested-session",
            "request": {
                "entries": [{"password": secrets[0]}, {"api_key": secrets[1]}],
                "credentials": secrets[2],
            },
        }
    )

    redacted = _redact_and_assert_removed(text, *secrets)
    payload = json.loads(redacted)

    assert payload["request"]["entries"][0]["password"] != secrets[0]
    assert payload["request"]["entries"][1]["api_key"] != secrets[1]
    assert payload["request"]["credentials"] != secrets[2]
    assert payload["max_tokens"] == 512
    assert payload["token_count"] == 27
    assert payload["uid"] == "nested-uid"
    assert payload["session_id"] == "nested-session"


def test_redact_log_text_masks_json_encoded_inside_a_string():
    secret = "json-string-api-key-secret"
    inner_json = json.dumps({"OPENAI_API_KEY": secret, "uid": "inner-uid"})
    text = json.dumps({"payload": inner_json, "session_id": "outer-session"})

    redacted = _redact_and_assert_removed(text, secret)
    payload = json.loads(redacted)
    inner_payload = json.loads(payload["payload"])

    assert inner_payload["OPENAI_API_KEY"] != secret
    assert inner_payload["uid"] == "inner-uid"
    assert payload["session_id"] == "outer-session"


def test_redact_log_text_masks_password_inside_json_encoded_note():
    text = "event + " + json.dumps({"note": 'password="escaped-secret"', "uid": "user-1"})

    redacted = _redact_and_assert_removed(text, "escaped-secret")
    json_start = redacted.index("{")
    json_end = redacted.rindex("}") + 1
    payload = json.loads(redacted[json_start:json_end])

    assert payload["note"] == 'password="[REDACTED]"'
    assert payload["uid"] == "user-1"


@pytest.mark.parametrize(
    "password_value",
    [
        '{"value":"container-secret"}',
        '[{"value":"container-secret"}]',
        '\'{"value":"container-secret"}\'',
    ],
)
def test_redact_log_text_masks_password_wrapping_embedded_json(password_value):
    text = f"event password={password_value}; count=2"

    redacted = _redact_and_assert_removed(text, "container-secret")

    assert redacted.endswith("; count=2")


def test_redact_log_text_masks_cookie_json_and_following_cookie_value():
    text = 'Cookie: session={"value":"cookie-json-secret"}; csrf=csrf-secret\ncount=2'

    redacted = _redact_and_assert_removed(text, "cookie-json-secret", "csrf-secret")

    assert redacted.endswith("\ncount=2")


def test_redact_log_text_masks_json_embedded_in_surrounding_text():
    secret = "embedded-json-password-secret"
    embedded_json = json.dumps({"password": secret, "max_tokens": 64, "token_count": 5})
    text = f"request payload: {embedded_json} completed"

    redacted = _redact_and_assert_removed(text, secret)

    assert redacted.startswith("request payload: ")
    assert redacted.endswith(" completed")
    json_start = redacted.index("{")
    json_end = redacted.rindex("}") + 1
    payload = json.loads(redacted[json_start:json_end])

    assert payload["max_tokens"] == 64
    assert payload["token_count"] == 5


@pytest.mark.parametrize(
    ("text", "secret", "metadata"),
    [
        (
            "event password=space separated password secret; max_tokens=32",
            "space separated password secret",
            "max_tokens=32",
        ),
        (
            'event password="double quoted password secret"; max_tokens=32',
            "double quoted password secret",
            "max_tokens=32",
        ),
        (
            "event api-key='single quoted api key secret'; max_tokens=32",
            "single quoted api key secret",
            "max_tokens=32",
        ),
        (
            r"event password=escaped\ password\ secret; max_tokens=32",
            r"escaped\ password\ secret",
            "max_tokens=32",
        ),
        (
            'event password="truncated quoted password secret',
            "truncated quoted password secret",
            None,
        ),
        ("event api_key=truncated-api-key-secret...", "truncated-api-key-secret...", None),
    ],
)
def test_redact_log_text_masks_key_value_secrets_with_varied_values(text, secret, metadata):
    redacted = _redact_and_assert_removed(text, secret)

    if metadata is not None:
        assert metadata in redacted


def test_redact_log_text_masks_complete_cookie_and_authorization_headers():
    secrets = ["cookie-session-secret", "cookie-csrf-secret", "header-bearer-secret"]
    text = "Cookie: sessionid=cookie-session-secret; csrftoken=cookie-csrf-secret\nAuthorization: Bearer header-bearer-secret"

    redacted = _redact_and_assert_removed(text, *secrets)

    assert "Cookie:" in redacted
    assert "Authorization:" in redacted


@pytest.mark.parametrize(
    ("scheme", "secret"),
    [
        ("Bearer", "standalone-bearer-secret"),
        ("Basic", "c2VjcmV0LWJhc2ljLWNyZWRlbnRpYWw="),
    ],
)
def test_redact_log_text_masks_bearer_and_basic_credentials(scheme, secret):
    text = f"request credential {scheme} {secret} accepted"
    redacted = _redact_and_assert_removed(text, secret)

    assert f"{scheme} " in redacted


def test_redact_log_text_masks_url_password_and_query_token():
    secrets = ["url-password-secret", "url-query-token-secret"]
    text = "GET https://alice:url-password-secret@example.test/resource?token=url-query-token-secret&page=2"

    redacted = _redact_and_assert_removed(text, *secrets)

    assert "example.test/resource" in redacted
    assert "page=2" in redacted


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (
            "-----BEGIN PRIVATE KEY-----\nfull-pem-private-key-secret\n-----END PRIVATE KEY-----",
            "full-pem-private-key-secret",
        ),
        (
            "-----BEGIN RSA PRIVATE KEY-----\ntruncated-pem-private-key-secret",
            "truncated-pem-private-key-secret",
        ),
    ],
)
def test_redact_log_text_masks_complete_and_truncated_pem_blocks(text, secret):
    _redact_and_assert_removed(text, secret)


def test_redact_log_text_leaves_ordinary_text_unchanged():
    text = "Routine request completed for uid=user-42 with max_tokens=32."

    redacted = _redact_and_assert_removed(text)

    assert redacted == text


def test_redact_log_text_leaves_ordinary_source_code_unchanged():
    text = 'const greeting = "hello"; return greeting.toUpperCase();'

    redacted = _redact_and_assert_removed(text)

    assert redacted == text
