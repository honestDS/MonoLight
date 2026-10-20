import json
from types import SimpleNamespace

import pytest

from app.core.log import LogManager
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
