import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger as loguru_logger

from app.core.log import LogManager, agent_log_context, get_logger
from app.core.log.redaction import redact_log_text
from app.core.paths import TOOLS_LOG_FILENAME
from app.tasks import _delete_rotated_log_files, _list_old_rotated_log_files


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


def _write_file_with_mtime(path: Path, content: str, modified_at: float) -> None:
    path.write_text(content, encoding="utf-8")
    os.utime(path, (modified_at, modified_at))


def _service_rotated_name(log_path: Path, timestamp: str, identifier=None, count=None) -> str:
    identifier_part = f".{identifier}" if identifier is not None else ""
    count_part = f".{count}" if count is not None else ""
    return f"{log_path.stem}{identifier_part}.{timestamp}{count_part}{log_path.suffix}.zip"


def _tools_rotated_name(timestamp: str, identifier=None, count=None) -> str:
    tools_path = Path(TOOLS_LOG_FILENAME)
    identifier_part = f".{identifier}" if identifier is not None else ""
    count_part = f".{count}" if count is not None else ""
    return f"{tools_path.stem}{identifier_part}.{timestamp}{count_part}{tools_path.suffix}"


@pytest.mark.parametrize("log_name", ["monolight.log", "service.[blue].txt", "service"])
def test_rotated_log_files_match_configured_service_and_tools_by_mtime(tmp_path, log_name):
    log_path = tmp_path / log_name
    cutoff = 2_000_000_000
    old_mtime = cutoff - 100
    new_mtime = cutoff + 100
    old_timestamp = "1999-01-01_00-00-00_000000"
    new_timestamp = "2099-12-31_23-59-59_999999"

    to_delete = {
        tmp_path / _service_rotated_name(log_path, old_timestamp): "service without identifier",
        tmp_path / _service_rotated_name(log_path, old_timestamp, identifier=7): "service numeric identifier",
        tmp_path / _service_rotated_name(log_path, old_timestamp, identifier=os.getpid()): "service current pid",
        tmp_path / _service_rotated_name(log_path, old_timestamp, count=2): "service rotation count",
        tmp_path / _service_rotated_name(log_path, new_timestamp, identifier=11): "filename date is new",
        tmp_path / _tools_rotated_name(old_timestamp): "tools without identifier",
        tmp_path / _tools_rotated_name(old_timestamp, identifier=13, count=2): "tools identifier and count",
    }
    for path, content in to_delete.items():
        _write_file_with_mtime(path, content, old_mtime)

    retained = {
        tmp_path / _service_rotated_name(log_path, old_timestamp, identifier=17): "old filename date but new mtime",
        tmp_path / _service_rotated_name(log_path, old_timestamp, identifier=19, count=2): "mtime cutoff boundary",
        log_path: "active log content",
    }
    _write_file_with_mtime(log_path, retained[log_path], old_mtime - 100 * 86400)
    _write_file_with_mtime(
        tmp_path / _service_rotated_name(log_path, old_timestamp, identifier=17),
        retained[tmp_path / _service_rotated_name(log_path, old_timestamp, identifier=17)],
        new_mtime,
    )
    _write_file_with_mtime(
        tmp_path / _service_rotated_name(log_path, old_timestamp, identifier=19, count=2),
        retained[tmp_path / _service_rotated_name(log_path, old_timestamp, identifier=19, count=2)],
        cutoff,
    )

    excluded = {
        tmp_path / f"{log_path.stem}.{old_timestamp}{log_path.suffix}": "service log still being compressed",
        tmp_path / "unrelated.zip": "unrelated zip",
        tmp_path / _service_rotated_name(Path("other.log"), old_timestamp): "other service prefix",
        tmp_path / f"{_service_rotated_name(log_path, old_timestamp)}.bak": "backup suffix",
    }
    for path, content in excluded.items():
        _write_file_with_mtime(path, content, old_mtime)

    nested_path = tmp_path / "nested" / _service_rotated_name(log_path, old_timestamp, identifier=23)
    nested_path.parent.mkdir()
    _write_file_with_mtime(nested_path, "nested file", old_mtime)

    selected = _list_old_rotated_log_files(log_path, cutoff=cutoff, batch_size=100)

    assert set(selected) == set(to_delete)
    assert _delete_rotated_log_files(selected, cutoff=cutoff) == len(to_delete)

    for path in to_delete:
        assert not path.exists()
    for path, content in retained.items():
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == content
    for path, content in excluded.items():
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == content
    assert nested_path.is_file()
    assert nested_path.read_text(encoding="utf-8") == "nested file"


def test_rotated_log_file_cleanup_respects_batch_limit_and_empty_directories(tmp_path):
    cutoff = 2_000_000_000
    log_path = tmp_path / "monolight.log"
    for index in range(4):
        path = tmp_path / _service_rotated_name(log_path, f"2000-01-01_00-00-0{index}_000000")
        _write_file_with_mtime(path, f"batch file {index}", cutoff - index - 1)

    first_batch = _list_old_rotated_log_files(log_path, cutoff=cutoff, batch_size=2)
    assert len(first_batch) == 2
    assert _delete_rotated_log_files(first_batch, cutoff=cutoff) == 2

    second_batch = _list_old_rotated_log_files(log_path, cutoff=cutoff, batch_size=2)
    assert len(second_batch) == 2
    assert _delete_rotated_log_files(second_batch, cutoff=cutoff) == 2

    assert _list_old_rotated_log_files(log_path, cutoff=cutoff, batch_size=2) == []
    assert _delete_rotated_log_files([], cutoff=cutoff) == 0

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert _list_old_rotated_log_files(empty_dir / "monolight.log", cutoff=cutoff, batch_size=2) == []

    missing_dir = tmp_path / "missing"
    assert _list_old_rotated_log_files(missing_dir / "monolight.log", cutoff=cutoff, batch_size=2) == []
    assert not missing_dir.exists()


def test_rotated_log_file_cleanup_rechecks_candidates_before_deleting(tmp_path):
    cutoff = 2_000_000_000
    log_path = tmp_path / "monolight.log"
    touched = tmp_path / _service_rotated_name(log_path, "2000-01-01_00-00-00_000000")
    disappeared = tmp_path / _service_rotated_name(log_path, "2000-01-01_00-00-01_000000")
    remaining = tmp_path / _service_rotated_name(log_path, "2000-01-01_00-00-02_000000")
    for path in (touched, disappeared, remaining):
        _write_file_with_mtime(path, path.name, cutoff - 1)

    candidates = _list_old_rotated_log_files(log_path, cutoff=cutoff, batch_size=3)
    os.utime(touched, (cutoff + 1, cutoff + 1))
    disappeared.unlink()

    assert _delete_rotated_log_files(candidates, cutoff=cutoff) == 1
    assert touched.is_file()
    assert touched.read_text(encoding="utf-8") == touched.name
    assert not disappeared.exists()
    assert not remaining.exists()


def test_rotated_log_file_cleanup_skips_symlinks_and_replaced_candidates(tmp_path):
    cutoff = 2_000_000_000
    log_path = tmp_path / "monolight.log"
    target = tmp_path / "target.log"
    symlink_path = tmp_path / _service_rotated_name(log_path, "2000-01-01_00-00-00_000000")
    _write_file_with_mtime(target, "symlink target", cutoff - 1)
    try:
        symlink_path.symlink_to(target)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    assert _list_old_rotated_log_files(log_path, cutoff=cutoff, batch_size=2) == []
    assert symlink_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "symlink target"

    candidate = tmp_path / _service_rotated_name(log_path, "2000-01-01_00-00-01_000000")
    replacement_target = tmp_path / "replacement-target.log"
    _write_file_with_mtime(candidate, "candidate content", cutoff - 1)
    _write_file_with_mtime(replacement_target, "replacement target", cutoff - 1)
    candidates = _list_old_rotated_log_files(log_path, cutoff=cutoff, batch_size=2)
    candidate.unlink()
    candidate.symlink_to(replacement_target)

    assert candidates == [candidate]
    assert _delete_rotated_log_files(candidates, cutoff=cutoff) == 0
    assert candidate.is_symlink()
    assert replacement_target.read_text(encoding="utf-8") == "replacement target"


def test_rotated_log_file_cleanup_continues_after_one_permission_error(tmp_path, monkeypatch):
    cutoff = 2_000_000_000
    log_path = tmp_path / "monolight.log"
    blocked = tmp_path / _service_rotated_name(log_path, "2000-01-01_00-00-00_000000")
    deleted = tmp_path / _service_rotated_name(log_path, "2000-01-01_00-00-01_000000")
    also_deleted = tmp_path / _service_rotated_name(log_path, "2000-01-01_00-00-02_000000")
    for path in (blocked, deleted, also_deleted):
        _write_file_with_mtime(path, path.name, cutoff - 1)

    original_unlink = Path.unlink

    def unlink_with_one_permission_error(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("simulated permission error")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_with_one_permission_error)
    candidates = _list_old_rotated_log_files(log_path, cutoff=cutoff, batch_size=3)

    assert _delete_rotated_log_files(candidates, cutoff=cutoff) == 2
    assert blocked.is_file()
    assert blocked.read_text(encoding="utf-8") == blocked.name
    assert not deleted.exists()
    assert not also_deleted.exists()

    monkeypatch.undo()
    remaining_candidates = _list_old_rotated_log_files(log_path, cutoff=cutoff, batch_size=3)
    assert _delete_rotated_log_files(remaining_candidates, cutoff=cutoff) == 1
    assert not blocked.exists()
