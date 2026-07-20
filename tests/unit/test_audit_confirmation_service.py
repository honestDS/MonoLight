import asyncio
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.adapters.weixin_openclaw.response import extract_event_reply
from app.core.audit.confirmation import ConfirmationDecision, message_has_quote, parse_confirmation_decision
from app.core.audit.service import (
    _apply_evidence_score_floor,
    _call_auditor,
    _collect_append_file_snapshots,
    _extract_script_execution_paths,
    _file_checks_are_sufficient,
    _file_snapshots_from_reads,
    _parse_results,
    _read_for_audit_sync,
    _requires_confirmation_from_evidence,
    _summarize_pending,
    audit_tool_round,
    classify_audit_score,
)
from app.core.constants import MSG_AUDIT_CONFIRMATION_IM, MSG_AUDIT_SCRIPT_SUMMARY_FALLBACK
from app.core.i18n import t
from app.core.message_platforms.inbound_collector import InboundMessageCollector
from app.core.prompts import AUDIT_BATCH_PROMPT
from app.core.utils.dispatcher.process_single_tool import prevalidate_tool_round
from app.models.audit import AuditToolConclusion
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from app.models.profile import ProfileConfig


def _profile_config() -> ProfileConfig:
    return ProfileConfig.model_validate(
        {
            "channel": {},
            "security": {},
            "tool": {
                "enabled_tools": ["execute_shell", "write_file"],
            },
            "other": {},
        }
    )


@pytest.mark.parametrize(
    ("score", "threshold", "expected"),
    [
        (0, 5, AuditToolConclusion.PASSED),
        (4, 5, AuditToolConclusion.PASSED),
        (5, 5, AuditToolConclusion.PENDING),
        (7, 5, AuditToolConclusion.PENDING),
        (0, 0, AuditToolConclusion.PASSED),
        (7, 0, AuditToolConclusion.PASSED),
        (8, 0, AuditToolConclusion.BLOCKED),
        (10, 7, AuditToolConclusion.BLOCKED),
    ],
)
def test_audit_score_conclusion_uses_configured_confirmation_threshold(score, threshold, expected):
    assert classify_audit_score(score, threshold) == expected


@pytest.mark.parametrize(
    ("score", "threshold", "requires_confirmation", "expected"),
    [
        (1, 5, True, 5),
        (6, 5, True, 6),
        (1, 5, False, 1),
        (1, 0, True, 1),
    ],
)
def test_evidence_score_floor_respects_disabled_confirmation(score, threshold, requires_confirmation, expected):
    assert _apply_evidence_score_floor(score, threshold, requires_confirmation=requires_confirmation) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python task.py", ("task.py",)),
        ("python3 -u scripts/task.py", ("scripts/task.py",)),
        ("python -W ignore task.py", ("task.py",)),
        (r'.venv\Scripts\python.exe "jobs\my task.py"', (r"jobs\my task.py",)),
        ("py -3 task.py", ("task.py",)),
        ("node app.js", ("app.js",)),
        ("bash -x deploy.sh", ("deploy.sh",)),
        ("sh scripts/check.sh", ("scripts/check.sh",)),
        ('powershell -File "scripts/task.ps1"', ("scripts/task.ps1",)),
        ("pwsh -f task.ps1", ("task.ps1",)),
        ("./task.py && node next.js", ("./task.py", "next.js")),
        ("task.bat", ("task.bat",)),
        ("call task.cmd", ("task.cmd",)),
        ("python -c \"print('ok')\"", ()),
        ("python -m package", ()),
        ('python "$SCRIPT"', ()),
        ("echo task.py", ()),
    ],
)
def test_script_execution_path_recognition(command, expected):
    assert _extract_script_execution_paths(command) == expected


def test_audit_prompt_requires_script_content_evidence():
    assert "MUST call read_text_file for every explicitly named script" in AUDIT_BATCH_PROMPT
    assert "This requirement cannot be skipped" in AUDIT_BATCH_PROMPT
    assert "explicit_script_paths" in AUDIT_BATCH_PROMPT
    assert "credential-stealing" not in AUDIT_BATCH_PROMPT
    assert "may transmit passwords to an external destination" in AUDIT_BATCH_PROMPT
    assert "its score must not be lower than 1" in AUDIT_BATCH_PROMPT
    assert "Script execution is not a read-only operation" not in AUDIT_BATCH_PROMPT


def test_audit_report_language_defaults_and_rejects_unsupported_locale():
    assert _profile_config().security.audit_report_language == "zh"
    assert _profile_config().security.audit_confirmation_timeout_seconds == 600

    with pytest.raises(ValueError):
        ProfileConfig.model_validate({"security": {"audit_report_language": "unsupported"}})


@pytest.mark.parametrize("timeout", [1, 86400])
def test_audit_confirmation_timeout_accepts_configured_boundaries(timeout):
    cfg = ProfileConfig.model_validate({"security": {"audit_confirmation_timeout_seconds": timeout}})

    assert cfg.security.audit_confirmation_timeout_seconds == timeout


@pytest.mark.parametrize("timeout", [0, 86401, 1.5, "10", True])
def test_audit_confirmation_timeout_rejects_invalid_values(timeout):
    with pytest.raises(ValueError):
        ProfileConfig.model_validate({"security": {"audit_confirmation_timeout_seconds": timeout}})


@pytest.mark.parametrize(
    ("config", "expected_seconds"),
    [
        ({"security": {"audit_confirmation_timeout_minutes": 10}}, 600),
        ({"audit_confirmation_timeout_minutes": 10}, 600),
        ({"configs": {"security": {"audit_confirmation_timeout_minutes": 10}}}, 600),
    ],
)
def test_legacy_confirmation_timeout_is_normalized_to_seconds(config, expected_seconds):
    cfg = ProfileConfig.model_validate(config)

    assert cfg.security.audit_confirmation_timeout_seconds == expected_seconds
    assert "audit_confirmation_timeout_seconds" in cfg.model_dump()["security"]
    assert "audit_confirmation_timeout_minutes" not in cfg.model_dump()["security"]


@pytest.mark.parametrize(
    "config",
    [
        {"security": {"audit_confirmation_timeout_minutes": 10, "audit_confirmation_timeout_seconds": 90}},
        {"audit_confirmation_timeout_minutes": 10, "audit_confirmation_timeout_seconds": 90},
    ],
)
def test_new_confirmation_timeout_takes_precedence_over_legacy_value(config):
    cfg = ProfileConfig.model_validate(config)

    assert cfg.security.audit_confirmation_timeout_seconds == 90


@pytest.mark.asyncio
async def test_pending_audit_uses_configured_confirmation_timeout(monkeypatch):
    import app.core.audit.service as service

    cfg = _profile_config()
    cfg.security.audit_confirmation_timeout_seconds = 25
    fixed_now = datetime(2026, 7, 18, 17, 0, 0)
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def call_auditor(*_args, **_kwargs):
        return {"messages": []}, {"parsed": {"results": [{"tool_call_id": "call-1", "score": 5, "reason": "confirm", "file_checks": []}]}}

    async def summarize_pending(*_args, **_kwargs):
        return "Confirm command", {}

    async def persist(*_args, **kwargs):
        captured["expires_at"] = kwargs["expires_at"]
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "_summarize_pending", summarize_pending)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)
    monkeypatch.setattr(service, "get_local_time", lambda: fixed_now)

    result = await audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "echo ok"})],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="zh",
        working_directory=".",
    )

    assert result.status.value == "pending"
    expected_expires_at = fixed_now + timedelta(seconds=25)
    assert captured["expires_at"] == expected_expires_at
    assert result.confirmation_payload["expires_at"] == expected_expires_at.isoformat()
    assert "25 秒后失效" in result.confirmation_payload["plain_text"]
    assert expected_expires_at.isoformat() not in result.confirmation_payload["plain_text"]


def test_english_confirmation_text_uses_relative_timeout_without_absolute_timestamp():
    text = t(MSG_AUDIT_CONFIRMATION_IM, locale="en", summary="Confirm command", score=5, expires_in_seconds=600)

    assert "Expires in 600 seconds" in text
    assert "Expires:" not in text


@pytest.mark.asyncio
async def test_passed_script_summary_uses_non_confirmation_i18n_fallback(monkeypatch):
    cfg = _profile_config()

    async def missing_channel(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.core.audit.service.channel_crud.get", missing_channel)

    summary, context = await _summarize_pending(
        SimpleNamespace(),
        cfg,
        [{"id": "call-1", "name": "execute_shell", "arguments": {"command": "python task.py"}}],
        confirmation_required=False,
    )

    assert summary == t(MSG_AUDIT_SCRIPT_SUMMARY_FALLBACK, locale="zh")
    assert context == {"fallback": True}


@pytest.mark.asyncio
async def test_missing_audit_configuration_fails_the_round_and_returns_one_result_per_tool(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    result = await audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[InternalToolCall(id="call-1", name="echo", arguments={"value": "ok"}), InternalToolCall(id="call-2", name="echo", arguments={"value": "ok"})],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
    )

    assert result.status.value == "audit_failed"
    assert len(result.tool_results) == 2
    assert captured["failure_type"].value == "audit_service_failed"
    assert all(json.loads(item.content)["status"] == "audit_failed" for item in result.tool_results)


@pytest.mark.asyncio
async def test_persistence_failure_does_not_reaccess_expired_record(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()

    class ExpiringRecord:
        expired = False

        @property
        def id(self):
            if self.expired:
                raise RuntimeError("expired ORM record accessed")
            return 321

    record = ExpiringRecord()

    class FakeDb:
        async def commit(self):
            return None

    async def no_cancellation(*_args, **_kwargs):
        return 0

    async def create_preparing(*_args, **_kwargs):
        return record

    async def call_auditor(*_args, **_kwargs):
        return {"messages": []}, {"parsed": {"results": [{"tool_call_id": "call-1", "score": 0, "reason": "safe", "file_checks": []}]}}

    async def fail_persistence(*_args, **_kwargs):
        record.expired = True
        return False

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_cancellation)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "persist_prepared_audit_round", fail_persistence)

    result = await audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "echo ok"})],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
    )

    assert result.audit_record_id == 321
    assert result.status.value == "audit_failed"
    assert json.loads(result.tool_results[0].content)["status"] == "audit_failed"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("同意", ConfirmationDecision.APPROVE),
        ("  continue  ", ConfirmationDecision.APPROVE),
        ("APPROVE", ConfirmationDecision.APPROVE),
        ("拒绝", ConfirmationDecision.REJECT),
        ("Reject", ConfirmationDecision.REJECT),
        ("同意执行", None),
        ("approve continue", None),
        ("", None),
    ],
)
def test_confirmation_words_are_strict(text, expected):
    assert parse_confirmation_decision(text) == expected


def test_confirmation_rejects_attachments_and_non_text():
    assert parse_confirmation_decision("同意", attachments=["file.txt"]) is None
    assert parse_confirmation_decision([{"type": "text", "text": "同意"}]) is None
    assert parse_confirmation_decision("同意", has_quote=True) is None
    assert message_has_quote({"quote": {"content": "previous"}}) is True
    assert message_has_quote({"item_list": [{"type": "reference"}]}) is True
    assert message_has_quote({"item_list": [{"type": "text"}]}) is False


def test_batch_result_parser_requires_exact_call_mapping_and_file_results():
    parsed = _parse_results(
        {
            "results": [
                {"tool_call_id": "call-1", "score": 1, "reason": "safe", "file_checks": []},
                {"tool_call_id": "call-2", "score": 5, "reason": "confirm", "file_checks": []},
            ]
        },
        ["call-1", "call-2"],
    )
    assert [item["tool_call_id"] for item in parsed] == ["call-1", "call-2"]

    with pytest.raises(ValueError):
        _parse_results(
            {
                "results": [
                    {"tool_call_id": "call-1", "score": 1, "reason": "safe", "file_checks": []},
                    {"tool_call_id": "call-1", "score": 1, "reason": "safe", "file_checks": []},
                ]
            },
            ["call-1", "call-2"],
        )

    with pytest.raises(ValueError):
        _parse_results(
            {"results": [{"tool_call_id": "call-1", "score": 1, "reason": "safe"}]},
            ["call-1"],
        )


def test_audit_file_reader_accepts_any_path_but_requires_round_tool_id(tmp_path):
    outside_file = tmp_path.parent / "audit-anywhere.txt"
    outside_file.write_text("unrestricted evidence", encoding="utf-8")
    state = {"bytes": 0, "calls": 0}

    result = _read_for_audit_sync(str(outside_file), "call-1", {"call-1"}, tmp_path, state)
    invalid = _read_for_audit_sync(str(outside_file), "other-call", {"call-1"}, tmp_path, state)

    assert result["status"] == "ok"
    assert result["content"] == "unrestricted evidence"
    assert result["tool_call_id"] == "call-1"
    assert invalid["status"] == "invalid"
    assert state == {"bytes": len("unrestricted evidence"), "calls": 2}


def test_file_checks_are_bound_to_the_actual_read_snapshot(tmp_path):
    source = tmp_path / "entry.txt"
    source.write_text("evidence", encoding="utf-8")
    read = _read_for_audit_sync(str(source), "call-1", {"call-1"}, tmp_path, {"bytes": 0, "calls": 0})
    snapshots = _file_snapshots_from_reads([read], {"call-1"})["call-1"]
    valid_check = {key: read[key] for key in ("original_path", "absolute_path", "resolved_path", "exists", "file_type", "status", "size", "sha256", "truncated")}

    assert _file_checks_are_sufficient(snapshots, [read], [valid_check])
    assert not _file_checks_are_sufficient(snapshots, [read], [{**valid_check, "sha256": "0" * 64}])
    assert _requires_confirmation_from_evidence(InternalToolCall(id="call-1", name="execute_shell", arguments={}), snapshots, [read], [])
    assert _requires_confirmation_from_evidence(InternalToolCall(id="call-1", name="execute_shell", arguments={}), [], [], [valid_check])
    assert not _requires_confirmation_from_evidence(InternalToolCall(id="call-1", name="execute_shell", arguments={}), [], [], [])


def test_append_file_snapshot_records_missing_target_and_ignores_non_append_or_outside_paths(tmp_path):
    calls = [
        InternalToolCall(id="append", name="write_file", arguments={"file_path": "nested/new.txt", "content": "new", "append": True}),
        InternalToolCall(id="overwrite", name="write_file", arguments={"file_path": "overwrite.txt", "content": "new", "append": False}),
        InternalToolCall(id="outside", name="write_file", arguments={"file_path": "../outside.txt", "content": "new", "append": True}),
    ]

    snapshots = _collect_append_file_snapshots(calls, tmp_path)

    snapshot = snapshots["append"][0]
    assert snapshot["absolute_path"] == str((tmp_path / "nested" / "new.txt").resolve())
    assert snapshot["exists"] is False
    assert snapshot["status"] == "missing"
    assert snapshot["size"] is None
    assert snapshot["sha256"] is None
    assert "overwrite" not in snapshots
    assert "outside" not in snapshots


def test_append_file_snapshot_records_missing_target_before_workspace_exists(tmp_path):
    workspace = tmp_path / "temp_new_user"
    tool_call = InternalToolCall(id="append", name="write_file", arguments={"file_path": "nested/new.txt", "content": "new", "append": True})

    snapshots = _collect_append_file_snapshots([tool_call], workspace)

    snapshot = snapshots["append"][0]
    assert workspace.exists() is False
    assert snapshot["absolute_path"] == str(workspace / "nested" / "new.txt")
    assert snapshot["exists"] is False
    assert snapshot["file_type"] == "missing"


def test_append_file_snapshot_preserves_link_type_and_resolved_path(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("content", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试链接: {exc}")

    tool_call = InternalToolCall(id="call-1", name="write_file", arguments={"file_path": "link.txt", "content": "append", "append": True})
    snapshots = _collect_append_file_snapshots([tool_call], tmp_path)

    snapshot = snapshots[tool_call.id][0]
    assert snapshot["absolute_path"] == str(link)
    assert snapshot["resolved_path"] == str(target.resolve())
    assert snapshot["exists"] is True
    assert snapshot["file_type"] == "symlink"
    assert snapshot["status"] == "ok"


@pytest.mark.asyncio
async def test_auditor_can_read_arbitrary_file_and_sees_complete_round_context(tmp_path, monkeypatch):
    outside_file = tmp_path.parent / "audit-visible.txt"
    outside_file.write_text("server evidence", encoding="utf-8")
    cfg = _profile_config()
    cfg.security.audit_channel_id = 1
    cfg.security.audit_model_id = "audit-model"
    calls = []

    class FakeDb:
        async def commit(self):
            return None

    class FakeChannel:
        is_active = True
        base_url = "https://audit.example"
        protocol = "openai"

        def get_decrypted_api_key(self):
            return "secret"

    async def get_channel(*_args, **_kwargs):
        return FakeChannel()

    async def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="read-1",
                            name="read_text_file",
                            arguments={"path": str(outside_file), "tool_call_id": "call-1"},
                        )
                    ],
                ),
                model="audit-model",
            )
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content='{"results":[{"tool_call_id":"call-1","score":0,"reason":"safe","file_checks":[]}]}',
            ),
            model="audit-model",
        )

    monkeypatch.setattr("app.core.audit.service.channel_crud.get", get_channel)
    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)

    request_context, response_context = await _call_auditor(
        FakeDb(),
        cfg,
        {
            "confirmation_threshold": 5,
            "working_directory": str(tmp_path),
            "tool_calls": [
                {
                    "tool_call_id": "call-1",
                    "turn_index": 0,
                    "tool_name": "execute_shell",
                    "arguments": {"command": "python dynamic_target"},
                }
            ],
        },
        tmp_path,
    )

    assert len(calls) == 2
    assert calls[0]["tools"][0]["function"]["name"] == "read_text_file"
    assert all(tool["function"]["name"] == "read_text_file" for tool in calls[1]["tools"])
    assert json.loads(calls[0]["messages"][1].content)["working_directory"] == str(tmp_path)
    assert json.loads(calls[0]["messages"][1].content)["tool_calls"][0]["arguments"] == {"command": "python dynamic_target"}
    assert response_context["file_reads"][0]["content"] == "server evidence"
    assert response_context["file_reads"][0]["tool_call_id"] == "call-1"
    assert any(message["role"] == "tool" for message in request_context["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_case", ["invalid_id", "wrong_tool", "missing_id"])
@pytest.mark.parametrize(("audit_threshold", "expected_status"), [(5, "pending"), (0, "passed")])
async def test_audit_round_handles_unassociated_read_protocol_failure_by_threshold(monkeypatch, tmp_path, protocol_case, audit_threshold, expected_status):
    import app.core.audit.service as service

    cfg = _profile_config()
    cfg.security.audit_channel_id = 1
    cfg.security.audit_model_id = "audit-model"
    cfg.security.audit_threshold = audit_threshold
    captured = {}
    calls = []
    logs = []

    class FakeBoundLogger:
        def info(self, message):
            logs.append(message)

    class FakeLogger:
        def bind(self, **_kwargs):
            return FakeBoundLogger()

    class FakeDb:
        async def commit(self):
            return None

    class FakeChannel:
        is_active = True
        base_url = "https://audit.example"
        protocol = "openai"

        def get_decrypted_api_key(self):
            return "secret"

    async def get_channel(*_args, **_kwargs):
        return FakeChannel()

    async def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            if protocol_case == "wrong_tool":
                name = "unexpected_reader"
                arguments = {"path": "missing.txt", "tool_call_id": "call-1"}
            elif protocol_case == "missing_id":
                name = "read_text_file"
                arguments = {"path": "missing.txt"}
            else:
                name = "read_text_file"
                arguments = {"path": "missing.txt", "tool_call_id": "other-call"}
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="read-1", name=name, arguments=arguments)],
                ),
                model="audit-model",
            )
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content='{"results":[{"tool_call_id":"call-1","score":0,"reason":"safe","file_checks":[]},{"tool_call_id":"call-2","score":0,"reason":"safe","file_checks":[]}]}',
            ),
            model="audit-model",
        )

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def summarize_pending(*_args, **_kwargs):
        return "Confirm command", {}

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "channel_crud", SimpleNamespace(get=get_channel))
    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_summarize_pending", summarize_pending)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)
    monkeypatch.setattr(service.LLMClient, "generate", generate)
    monkeypatch.setattr(service, "logger", FakeLogger())

    result = await service.audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[
            InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "echo ok"}),
            InternalToolCall(id="call-2", name="execute_shell", arguments={"command": "echo also-ok"}),
        ],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
    )

    assert result.status.value == expected_status
    assert all(detail["conclusion"] == expected_status for detail in captured["tool_details"])
    assert all(detail["score"] == audit_threshold for detail in captured["tool_details"])
    assert all(detail["server_confirmation_reasons"][-1]["code"] == "file_read_protocol_invalid" for detail in captured["tool_details"])
    assert len(logs) == 2
    assert "安全审计 LLM" in logs[0] or "Security audit LLM" in logs[0]
    assert "record_id=123" in logs[0]
    assert "record_id=123" in logs[1]
    assert f"status={expected_status}" in logs[1]
    assert f"max_score={audit_threshold}" in logs[1]
    assert f"summary={'Confirm command' if expected_status == 'pending' else '-'}" in logs[1]
    if audit_threshold == 0:
        assert result.tool_results == ()
        assert result.confirmation_payload is None


def test_audit_file_reader_enforces_total_bytes_and_call_count(monkeypatch, tmp_path):
    import app.core.audit.service as service

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("12345", encoding="utf-8")
    second.write_text("abcdef", encoding="utf-8")
    monkeypatch.setattr(service, "AUDIT_FILE_MAX_BYTES", 4)
    monkeypatch.setattr(service, "AUDIT_FILE_TOTAL_BYTES", 6)
    monkeypatch.setattr(service, "AUDIT_FILE_MAX_CALLS", 2)
    state = {"bytes": 0, "calls": 0}

    first_result = service._read_for_audit_sync(str(first), "call-1", {"call-1"}, tmp_path, state)
    second_result = service._read_for_audit_sync(str(second), "call-1", {"call-1"}, tmp_path, state)
    third_result = service._read_for_audit_sync(str(first), "call-1", {"call-1"}, tmp_path, state)

    assert first_result["content"] == "1234"
    assert second_result["content"] == "ab"
    assert first_result["truncated"] is True
    assert second_result["truncated"] is True
    assert third_result["status"] == "limit_exceeded"
    assert state == {"bytes": 6, "calls": 3}


@pytest.mark.parametrize("file_kind", ["missing", "truncated", "non_utf8"])
def test_read_failure_snapshots_are_rechecked_before_confirmed_execution(monkeypatch, tmp_path, file_kind):
    import app.core.audit.service as service
    from app.core.session_reply_queue import executor as executor_module

    monkeypatch.setattr(service, "AUDIT_FILE_MAX_BYTES", 4)
    target = tmp_path / f"{file_kind}.txt"
    if file_kind == "truncated":
        target.write_bytes(b"0123456789")
    elif file_kind == "non_utf8":
        target.write_bytes(b"\xff\xfe")

    read = service._read_for_audit_sync(str(target), "call-1", {"call-1"}, tmp_path, {"bytes": 0, "calls": 0})
    snapshots = service._file_snapshots_from_reads([read], {"call-1"})["call-1"]
    assert len(snapshots) == 1
    assert snapshots[0]["absolute_path"] == str(target)
    if file_kind == "missing":
        assert snapshots[0]["exists"] is False
        assert snapshots[0]["file_type"] == "missing"
    elif file_kind == "truncated":
        assert snapshots[0]["status"] == "ok"
        assert snapshots[0]["truncated"] is True
        assert snapshots[0]["sha256"]
    else:
        assert snapshots[0]["status"] == "unreadable"
        assert snapshots[0]["size"] == 2
        assert snapshots[0]["sha256"]

    details = [SimpleNamespace(file_snapshots=snapshots)]
    assert not executor_module._confirmed_file_snapshots_changed(details, working_directory=str(tmp_path))
    if file_kind == "missing":
        target.write_text("created", encoding="utf-8")
    elif file_kind == "truncated":
        target.write_bytes(b"changed-data")
    else:
        target.write_bytes(b"\xfd\xfb")
    assert executor_module._confirmed_file_snapshots_changed(details, working_directory=str(tmp_path))


@pytest.mark.parametrize("file_type", ["regular_file", "other"])
def test_unstable_read_failures_require_confirmation_without_persisted_snapshot(tmp_path, file_type):
    path = tmp_path / "unreadable"
    file_read = {
        "tool_call_id": "call-1",
        "original_path": str(path),
        "absolute_path": str(path),
        "resolved_path": str(path.resolve(strict=False)),
        "exists": True,
        "file_type": file_type,
        "status": "unreadable" if file_type == "regular_file" else "not_regular",
        "truncated": False,
        "error": "unavailable",
    }

    snapshots = _file_snapshots_from_reads([file_read], {"call-1"})

    assert snapshots == {}
    assert _requires_confirmation_from_evidence(InternalToolCall(id="call-1", name="execute_shell", arguments={}), [], [file_read], [])


def test_append_file_snapshot_converts_integrity_errors_to_conservative_snapshot(monkeypatch, tmp_path):
    import app.core.audit.service as service

    def fail_snapshot(*_args, **_kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(service, "create_file_integrity_snapshot", fail_snapshot)
    call = InternalToolCall(id="append", name="write_file", arguments={"file_path": "target.txt", "content": "new", "append": True})

    snapshots = service._collect_append_file_snapshots([call], tmp_path)

    snapshot = snapshots["append"][0]
    assert snapshot["status"] == "unreadable"
    assert snapshot["file_type"] == "unknown"
    assert snapshot["exists"] is None
    assert "permission denied" in snapshot["error"]


@pytest.mark.asyncio
async def test_read_snapshot_is_bound_to_tool_detail_and_missing_check_requires_confirmation(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()
    source = tmp_path / "evidence.txt"
    source.write_text("review me", encoding="utf-8")
    read = _read_for_audit_sync(str(source), "call-1", {"call-1", "call-2"}, tmp_path, {"bytes": 0, "calls": 0})
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def call_auditor(*_args, **_kwargs):
        return {"messages": []}, {
            "file_reads": [read],
            "parsed": {
                "results": [
                    {"tool_call_id": "call-1", "score": 0, "reason": "review me", "file_checks": []},
                    {"tool_call_id": "call-2", "score": 0, "reason": "safe", "file_checks": []},
                ]
            },
        }

    async def summarize_pending(*_args, **_kwargs):
        return "Confirm command", {}

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "_summarize_pending", summarize_pending)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    result = await service.audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[
            InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "python evidence.txt"}),
            InternalToolCall(id="call-2", name="execute_shell", arguments={"command": "echo ok"}),
        ],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
    )

    assert result.status.value == "pending"
    first_detail, second_detail = captured["tool_details"]
    assert first_detail["file_snapshots"][0]["sha256"] == read["sha256"]
    assert "content" not in first_detail["file_snapshots"][0]
    assert second_detail["file_snapshots"] == []
    assert first_detail["server_confirmation_reasons"][0]["code"] == "file_evidence_insufficient"
    assert "review me" not in first_detail["reason"]


@pytest.mark.asyncio
async def test_model_can_score_dynamic_command_without_reading(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def call_auditor(*args, **_kwargs):
        captured["request_payload"] = args[2]
        return {"messages": []}, {"file_reads": [], "parsed": {"results": [{"tool_call_id": "call-1", "score": 1, "reason": "safe without a read", "file_checks": []}]}}

    async def persist(*_args, **kwargs):
        captured["tool_details"] = kwargs["tool_details"]
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    command = 'python "$SCRIPT"'
    result = await service.audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": command})],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
    )

    assert result.status.value == "passed"
    assert captured["request_payload"]["working_directory"] == str(tmp_path.resolve())
    assert captured["request_payload"]["tool_calls"][0]["arguments"] == {"command": command}
    assert captured["tool_details"][0]["file_snapshots"] == []


@pytest.mark.asyncio
async def test_readable_script_without_matching_read_evidence_requires_confirmation(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()
    script = tmp_path / "task.py"
    script.write_text("print('safe')\n", encoding="utf-8")
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def call_auditor(*args, **_kwargs):
        captured["request_payload"] = args[2]
        return {"messages": []}, {"file_reads": [], "parsed": {"results": [{"tool_call_id": "call-1", "score": 0, "reason": "safe", "file_checks": []}]}}

    async def summarize_pending(*_args, **_kwargs):
        return "Run task.py", {"response": "Run task.py"}

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "_summarize_pending", summarize_pending)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    result = await service.audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "python task.py"})],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
    )

    assert result.status.value == "pending"
    assert result.confirmation_payload["type"] == "audit_confirmation"
    assert captured["request_payload"]["tool_calls"][0]["explicit_script_paths"] == ["task.py"]
    assert captured["tool_details"][0]["score"] == cfg.security.audit_threshold
    reason = captured["tool_details"][0]["server_confirmation_reasons"][0]
    assert reason["code"] == "file_evidence_insufficient"
    assert reason["details"]["missing_script_paths"] == ["task.py"]


@pytest.mark.asyncio
async def test_safe_script_zero_score_is_preserved_and_summary_persisted_without_confirmation(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()
    script = tmp_path / "task.py"
    script.write_text("print('safe')\n", encoding="utf-8")
    read = _read_for_audit_sync("task.py", "call-1", {"call-1"}, tmp_path, {"bytes": 0, "calls": 0})
    file_check = {key: read[key] for key in ("original_path", "absolute_path", "resolved_path", "exists", "file_type", "status", "size", "sha256", "truncated")}
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=124)

    async def call_auditor(*_args, **_kwargs):
        return {"messages": []}, {
            "file_reads": [read],
            "parsed": {"results": [{"tool_call_id": "call-1", "score": 0, "reason": "safe script", "file_checks": [file_check]}]},
        }

    async def summarize_pending(*_args, **kwargs):
        assert kwargs["confirmation_required"] is False
        return "Run the safe task.py script", {"response": "Run the safe task.py script"}

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "_summarize_pending", summarize_pending)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    result = await service.audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "python task.py"})],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
    )

    assert result.status.value == "passed"
    assert result.confirmation_payload is None
    assert result.tool_results == ()
    assert captured["tool_details"][0]["score"] == 0
    assert captured["intent_summary"] == "Run the safe task.py script"
    assert captured["context_payload"]["summary"] == {"response": "Run the safe task.py script"}
    assert captured["expires_at"] is None


@pytest.mark.asyncio
async def test_zero_score_for_ordinary_command_stays_zero_without_summary_or_confirmation(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=125)

    async def call_auditor(*_args, **_kwargs):
        return {"messages": []}, {"file_reads": [], "parsed": {"results": [{"tool_call_id": "call-1", "score": 0, "reason": "read only", "file_checks": []}]}}

    async def summarize_pending(*_args, **_kwargs):
        raise AssertionError("ordinary low-risk commands must not be summarized")

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "_summarize_pending", summarize_pending)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    result = await service.audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "echo ok"})],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
    )

    assert result.status.value == "passed"
    assert result.confirmation_payload is None
    assert captured["tool_details"][0]["score"] == 0
    assert captured["intent_summary"] is None
    assert "summary" not in captured["context_payload"]


@pytest.mark.asyncio
async def test_pending_summary_can_read_shell_script_and_uses_configured_report_language(monkeypatch, tmp_path):
    cfg = _profile_config()
    cfg.security.audit_channel_id = 1
    cfg.security.audit_model_id = "audit-model"
    cfg.security.audit_report_language = "en"
    captured = {}
    calls = []
    script = tmp_path / "test.py"
    script.write_text("from pathlib import Path\nPath('important.txt').unlink()\n", encoding="utf-8")

    class FakeDb:
        async def commit(self):
            return None

    class FakeChannel:
        is_active = True
        base_url = "https://audit.example"
        protocol = "openai"

        def get_decrypted_api_key(self):
            return "secret"

    async def get_channel(*_args, **_kwargs):
        return FakeChannel()

    async def generate(**kwargs):
        calls.append(kwargs)
        captured.update(kwargs)
        if len(calls) == 1:
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[InternalToolCall(id="read-1", name="read_text_file", arguments={"path": "test.py", "tool_call_id": "call-1"})],
                ),
                model="audit-model",
            )
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="Run test.py, which deletes important.txt; execution is awaiting confirmation."),
            model="audit-model",
        )

    monkeypatch.setattr("app.core.audit.service.channel_crud.get", get_channel)
    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)

    reasons = {
        "call-1": [
            {
                "code": "file_evidence_insufficient",
                "message": "Server-side file evidence read for this tool call was incomplete or not verified by the audit result",
                "details": {"snapshot_count": 1, "model_file_check_count": 0, "server_file_read_count": 1},
            }
        ]
    }
    summary, _context = await _summarize_pending(
        FakeDb(),
        cfg,
        [{"id": "call-1", "name": "execute_shell", "arguments": {"command": "python test.py"}}],
        reasons,
        working_directory=tmp_path,
    )

    system_prompt = calls[0]["messages"][0].content
    assert summary == "Run test.py, which deletes important.txt; execution is awaiting confirmation."
    assert len(calls) == 2
    assert calls[0]["tools"][0]["function"]["name"] == "read_text_file"
    tool_payload = json.loads(calls[1]["messages"][-1].content)
    assert "Path('important.txt').unlink()" in tool_payload["content"]
    assert "locale code: en" in system_prompt
    assert "entire sentence only in that language" in system_prompt
    assert "action, concrete target or path, intended effect" in system_prompt
    assert "what the command or target script actually does" in system_prompt
    assert "the script itself was not executed" in system_prompt
    assert "never reduce this to a vague phrase" in system_prompt
    assert json.loads(calls[0]["messages"][1].content) == {
        "working_directory": str(tmp_path.resolve()),
        "tool_calls": [{"id": "call-1", "name": "execute_shell", "arguments": {"command": "python test.py"}}],
        "server_confirmation_reasons": reasons,
    }


def test_tool_round_precheck_fails_entire_round_before_execution():
    cfg = _profile_config()
    calls = [
        SimpleNamespace(id="call-1", name="execute_shell", arguments={}),
        SimpleNamespace(id="call-2", name="write_file", arguments={"file_path": "ok.txt", "content": "ok"}),
    ]

    errors = prevalidate_tool_round(calls, cfg)

    assert set(errors) == {"call-1"}
    assert "command" in errors["call-1"]


@pytest.mark.asyncio
async def test_collector_flush_and_wait_finishes_old_batch_before_decision():
    order: list[str] = []
    old_dispatch_started = asyncio.Event()
    release_old_dispatch = asyncio.Event()

    async def dispatch(message: str) -> None:
        order.append(f"start:{message}")
        old_dispatch_started.set()
        await release_old_dispatch.wait()
        order.append(f"end:{message}")

    collector = InboundMessageCollector(
        quiet_period_seconds=10,
        max_wait_seconds=20,
        merge=lambda left, right: f"{left}{right}",
        dispatch=dispatch,
    )
    await collector.add("session-1", "old")
    waiting = asyncio.create_task(collector.flush_and_wait("session-1"))
    await old_dispatch_started.wait()
    assert not waiting.done()
    release_old_dispatch.set()
    await waiting
    order.append("decision")
    await collector.close()

    assert order == ["start:old", "end:old", "decision"]


def test_weixin_confirmation_event_uses_plain_text():
    text, files = extract_event_reply(
        {
            "content": '{"type":"audit_confirmation","plain_text":"Confirmation required","status":"pending"}',
            "files": [],
        }
    )

    assert text == "Confirmation required"
    assert files == []
