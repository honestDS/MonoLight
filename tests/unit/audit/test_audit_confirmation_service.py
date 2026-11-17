import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.adapters.weixin_openclaw.response import extract_event_reply
from app.core.audit.confirmation import ConfirmationDecision, is_confirmation_candidate, message_has_quote, parse_confirmation_decision
from app.core.audit.service import (
    _parse_results,
    _summarize_pending,
    audit_tool_round,
    classify_audit_score,
    is_audit_configured,
)
from app.core.constants import (
    DEFAULT_CHAT_MAX_TOKENS,
    MSG_AUDIT_CONFIRMATION_IM,
    MSG_AUDIT_HIGH_RISK_CONFIRMATION_IM,
    MSG_AUDIT_ROUND_SKIPPED,
    MSG_AUDIT_WAITING_CONFIRMATION,
)
from app.core.i18n import t
from app.core.message_platforms.inbound_collector import InboundMessageCollector
from app.core.prompts import AUDIT_SUMMARY_PROMPT
from app.core.utils.dispatcher.process_single_tool import prevalidate_tool_round
from app.core.utils.operation_directories import get_write_protected_dirs
from app.models.audit import AuditToolConclusion
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from app.models.profile import ProfileConfig
from tests.unit.response_test_support import audit_confirmation_payload, pending_audit_tool_result


def _profile_config() -> ProfileConfig:
    return ProfileConfig.model_validate(
        {
            "channel": {},
            "security": {
                "audit_channel_id": 1,
                "audit_model_id": "audit-model",
            },
            "tool": {
                "enabled_tools": ["execute_shell", "file_tool"],
            },
            "other": {},
        }
    )


def _auditor_response_context(results, *, file_reads=()):
    return {"messages": []}, {
        "content": json.dumps({"results": results}, ensure_ascii=False),
        "file_reads": list(file_reads),
        "shell_results": [],
        "parsed": {"results": results},
    }


@pytest.mark.parametrize(
    ("score", "threshold", "expected"),
    [
        (0, 5, AuditToolConclusion.PASSED),
        (4, 5, AuditToolConclusion.PASSED),
        (5, 5, AuditToolConclusion.PENDING),
        (7, 5, AuditToolConclusion.PENDING),
        (0, 0, AuditToolConclusion.PASSED),
        (7, 0, AuditToolConclusion.PASSED),
        (8, 0, AuditToolConclusion.PENDING),
        (10, 7, AuditToolConclusion.PENDING),
    ],
)
def test_audit_score_conclusion_uses_configured_confirmation_threshold(score, threshold, expected):
    assert classify_audit_score(score, threshold) == expected


def test_audit_report_language_defaults_and_rejects_unsupported_locale():
    assert _profile_config().security.audit_report_language == "zh"
    assert _profile_config().security.audit_confirmation_timeout_seconds == 600

    with pytest.raises(ValueError):
        ProfileConfig.model_validate({"security": {"audit_report_language": "unsupported"}})


@pytest.mark.parametrize(
    ("channel_id", "model_id", "expected"),
    [
        (1, "audit-model", True),
        (None, "audit-model", False),
        (1, None, False),
        (1, "   ", False),
        (0, "audit-model", False),
        (True, "audit-model", False),
    ],
)
def test_audit_configuration_requires_positive_integer_channel_and_nonblank_model(channel_id, model_id, expected):
    cfg = _profile_config()
    cfg.security.audit_channel_id = channel_id
    cfg.security.audit_model_id = model_id

    assert is_audit_configured(cfg) is expected


def test_audit_depth_defaults_to_three_and_is_serialized_in_security():
    cfg = ProfileConfig.model_validate({})

    assert cfg.security.audit_depth == 3
    assert cfg.model_dump()["security"]["audit_depth"] == 3


def test_protect_system_directories_defaults_to_true_and_is_serialized_in_security():
    cfg = ProfileConfig.model_validate({})

    assert cfg.security.protect_system_directories is True
    assert cfg.model_dump()["security"]["protect_system_directories"] is True


@pytest.mark.parametrize("shape", ["nested", "flat", "configswrapper"])
@pytest.mark.parametrize("value", [True, False])
def test_protect_system_directories_is_preserved_across_supported_config_shapes(shape, value):
    security = {"protect_system_directories": value}
    config = {
        "nested": {"security": security},
        "flat": security,
        "configswrapper": {"configs": {"security": security}},
    }[shape]
    cfg = ProfileConfig.model_validate(config)

    assert cfg.security.protect_system_directories is value
    dumped = cfg.model_dump()
    assert dumped["security"]["protect_system_directories"] is value
    assert ProfileConfig.model_validate(dumped).security.protect_system_directories is value


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false"])
def test_protect_system_directories_rejects_non_boolean_values(value):
    with pytest.raises(ValueError):
        ProfileConfig.model_validate({"security": {"protect_system_directories": value}})


@pytest.mark.parametrize(
    "config",
    [
        {"security": {"audit_depth": 6}},
        {"audit_depth": 6},
        {"configs": {"security": {"audit_depth": 6}}},
    ],
)
def test_audit_depth_is_preserved_across_supported_config_shapes(config):
    cfg = ProfileConfig.model_validate(config)

    assert cfg.security.audit_depth == 6
    dumped = cfg.model_dump()
    assert dumped["security"]["audit_depth"] == 6
    assert ProfileConfig.model_validate(dumped).security.audit_depth == 6


@pytest.mark.parametrize("audit_depth", [1, 100])
def test_audit_depth_accepts_configured_boundaries(audit_depth):
    cfg = ProfileConfig.model_validate({"security": {"audit_depth": audit_depth}})

    assert cfg.security.audit_depth == audit_depth


@pytest.mark.parametrize("audit_depth", [0, -1, 101, 1.5, "3", True, None])
def test_audit_depth_rejects_invalid_values(audit_depth):
    with pytest.raises(ValueError):
        ProfileConfig.model_validate({"security": {"audit_depth": audit_depth}})


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
        return _auditor_response_context([{"tool_call_id": "call-1", "score": 5, "reason": "confirm", "deny": False, "file_checks": []}])

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
        execution_scope="foreground",
    )

    assert result.status.value == "pending"
    assert len(result.tool_results) == 1
    expected_tool_result = {"status": "pending", "error": t(MSG_AUDIT_WAITING_CONFIRMATION)}
    actual_tool_result = result.tool_results[0]
    assert actual_tool_result.role == MessageRole.TOOL
    assert actual_tool_result.tool_call_id == "call-1"
    assert json.loads(actual_tool_result.content) == expected_tool_result
    expected_tool_result_message = pending_audit_tool_result("call-1")
    assert expected_tool_result_message.role == MessageRole.TOOL
    assert expected_tool_result_message.tool_call_id == "call-1"
    assert json.loads(expected_tool_result_message.content) == expected_tool_result

    expected_expires_at = fixed_now + timedelta(seconds=25)
    expected = {
        "type": "audit_confirmation",
        "audit_record_id": 123,
        "summary": "Confirm command",
        "risk": 5,
        "status": "pending",
        "confirmation_mode": "standard",
        "expires_at": expected_expires_at.isoformat(),
        "plain_text": t(
            MSG_AUDIT_CONFIRMATION_IM,
            locale="zh",
            summary="Confirm command",
            score=5,
            expires_in_seconds=25,
        ),
    }
    assert captured["expires_at"] == expected_expires_at
    assert result.confirmation_payload == expected
    assert audit_confirmation_payload(123, expires_at=expected_expires_at.isoformat()) == expected
    assert "25 秒后失效" in result.confirmation_payload["plain_text"]
    assert expected_expires_at.isoformat() not in result.confirmation_payload["plain_text"]


def test_english_confirmation_text_uses_relative_timeout_without_absolute_timestamp():
    text = t(MSG_AUDIT_CONFIRMATION_IM, locale="en", summary="Confirm command", score=5, expires_in_seconds=600)

    assert "Expires in 600 seconds" in text
    assert "Expires:" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_id", "model_id"),
    [
        (None, None),
        (1, None),
        (None, "audit-model"),
        (1, "   "),
    ],
)
async def test_missing_audit_configuration_skips_round_without_side_effects(monkeypatch, tmp_path, channel_id, model_id):
    import app.core.audit.service as service

    cfg = _profile_config()
    cfg.security.audit_channel_id = channel_id
    cfg.security.audit_model_id = model_id

    class FakeDb:
        async def commit(self):
            return None

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("unconfigured audit must not perform audit side effects")

    def unexpected_snapshot(*_args, **_kwargs):
        raise AssertionError("unconfigured audit must not collect evidence")

    monkeypatch.setattr(service, "cancel_confirmation_by_session", unexpected_call)
    monkeypatch.setattr(service.audit_crud, "create_preparing", unexpected_call)
    monkeypatch.setattr(service, "_call_auditor", unexpected_call)
    monkeypatch.setattr(service, "persist_prepared_audit_round", unexpected_call)
    monkeypatch.setattr(service, "_collect_file_mutation_snapshots", unexpected_snapshot)

    result = await audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[
            InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "echo ok"}),
            InternalToolCall(id="call-2", name="file_tool", arguments={"operation": "write", "path": "result.txt", "content": "ok"}),
        ],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
        execution_scope="foreground",
    )

    assert result is None


@pytest.mark.asyncio
async def test_configured_audit_skips_safe_tool_round_without_side_effects(monkeypatch, tmp_path):
    import app.core.audit.service as service

    class FakeDb:
        async def commit(self):
            return None

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("safe tools must not perform audit side effects")

    def unexpected_snapshot(*_args, **_kwargs):
        raise AssertionError("safe tools must not collect audit evidence")

    monkeypatch.setattr(service, "cancel_confirmation_by_session", unexpected_call)
    monkeypatch.setattr(service.audit_crud, "create_preparing", unexpected_call)
    monkeypatch.setattr(service, "_call_auditor", unexpected_call)
    monkeypatch.setattr(service, "persist_prepared_audit_round", unexpected_call)
    monkeypatch.setattr(service, "_collect_file_mutation_snapshots", unexpected_snapshot)
    monkeypatch.setattr(service, "build_tool_round_integrity_snapshot", unexpected_snapshot)

    result = await audit_tool_round(
        FakeDb(),
        cfg=_profile_config(),
        tool_calls=[
            InternalToolCall(id="call-1", name="manage_background_tasks", arguments={"operation": "list"}),
            InternalToolCall(id="call-2", name="web_tool", arguments={"operation": "search", "query": "Monolight"}),
        ],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
        execution_scope="foreground",
    )

    assert result is None


@pytest.mark.asyncio
async def test_shell_blacklist_blocks_audit_round_without_confirmation(monkeypatch, tmp_path):
    import app.core.audit.service as service

    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_cancellation(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("locally blocked shell commands must not call the auditor or summary model")

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_cancellation)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", unexpected_call)
    monkeypatch.setattr(service, "_summarize_pending", unexpected_call)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    result = await service.audit_tool_round(
        FakeDb(),
        cfg=_profile_config(),
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": 'powershell -NoProfile -Command "Write-Host ok"'})],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
        execution_scope="foreground",
    )

    assert result.status.value == "blocked"
    assert result.confirmation_payload is None
    assert captured["tool_details"][0]["conclusion"] == "blocked"
    assert json.loads(result.tool_results[0].content)["status"] == "blocked"


@pytest.mark.asyncio
async def test_same_round_file_write_conflict_blocks_without_confirmation(monkeypatch, tmp_path):
    import app.core.audit.service as service

    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_cancellation(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def unexpected_call(*_args, **_kwargs):
        raise AssertionError("locally blocked file conflicts must not call the auditor or summary model")

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_cancellation)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", unexpected_call)
    monkeypatch.setattr(service, "_summarize_pending", unexpected_call)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    result = await service.audit_tool_round(
        FakeDb(),
        cfg=_profile_config(),
        tool_calls=[
            InternalToolCall(id="call-1", name="file_tool", arguments={"operation": "write", "path": "result.txt", "content": "first"}),
            InternalToolCall(id="call-2", name="file_tool", arguments={"operation": "edit", "path": "result.txt", "old_text": "x", "new_text": "y"}),
        ],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
        execution_scope="foreground",
    )

    assert result.status.value == "blocked"
    assert result.confirmation_payload is None
    assert [detail["conclusion"] for detail in captured["tool_details"]] == ["blocked", "blocked"]
    assert [json.loads(tool_result.content)["status"] for tool_result in result.tool_results] == ["blocked", "blocked"]


@pytest.mark.asyncio
async def test_mixed_tool_round_audits_only_declared_calls_and_persists_full_round(monkeypatch, tmp_path):
    import app.core.audit.service as service

    cfg = _profile_config()
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_cancellation(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **kwargs):
        captured["tool_count"] = kwargs["tool_count"]
        return SimpleNamespace(id=123)

    async def call_auditor(*args, **_kwargs):
        captured["request_payload"] = args[2]
        return _auditor_response_context([{"tool_call_id": "risk-call", "score": 0, "reason": "safe", "deny": False, "file_checks": []}])

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_cancellation)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    result = await audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[
            InternalToolCall(id="safe-call", name="manage_background_tasks", arguments={"operation": "list"}),
            InternalToolCall(id="risk-call", name="execute_shell", arguments={"command": "echo ok"}),
        ],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
        execution_scope="foreground",
    )

    assert result.status.value == "passed"
    assert captured["tool_count"] == 2
    assert [item["tool_call_id"] for item in captured["request_payload"]["tool_calls"]] == ["risk-call"]
    assert [item["turn_index"] for item in captured["request_payload"]["tool_calls"]] == [1]
    assert [item["original_tool_call_id"] for item in captured["tool_details"]] == ["safe-call", "risk-call"]
    assert captured["tool_details"][0]["conclusion"] == "passed"
    assert captured["tool_details"][0]["score"] == 0
    assert captured["tool_details"][0]["reason"] == t(MSG_AUDIT_ROUND_SKIPPED)
    assert [item["name"] for item in captured["context_payload"]["tool_calls"]] == ["manage_background_tasks", "execute_shell"]
    assert captured["context_payload"]["audited_tool_call_ids"] == ["risk-call"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_case", ["channel_missing", "channel_inactive", "model_error", "invalid_response"])
async def test_configured_audit_runtime_failures_still_persist_audit_failed(monkeypatch, tmp_path, failure_case):
    import app.core.audit.service as service

    cfg = _profile_config()
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    class FakeChannel:
        id = 1
        name = "audit-channel"
        usage_key = "audit-channel"
        is_active = True
        base_url = "https://audit.example"
        model_ids = [{"model_id": "audit-model", "usage": "CHAT", "protocol": "OPENAI"}]

        def get_decrypted_api_key(self):
            return "secret"

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def get_channel(*_args, **_kwargs):
        if failure_case == "channel_missing":
            return None
        if failure_case == "channel_inactive":
            return SimpleNamespace(is_active=False)
        return FakeChannel()

    async def generate(**_kwargs):
        if failure_case == "model_error":
            raise RuntimeError("audit model failed")
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="not valid json"),
            model="audit-model",
        )

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)
    monkeypatch.setattr(service.channel_crud, "get", get_channel)
    monkeypatch.setattr(service.LLMClient, "generate", generate)

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
        execution_scope="foreground",
    )

    assert result is not None
    assert result.status.value == "audit_failed"
    assert captured["failure_type"].value == "audit_service_failed"
    assert json.loads(result.tool_results[0].content)["status"] == "audit_failed"


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
        return _auditor_response_context([{"tool_call_id": "call-1", "score": 0, "reason": "safe", "deny": False, "file_checks": []}])

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
        execution_scope="foreground",
    )

    assert result.audit_record_id == 321
    assert result.status.value == "audit_failed"
    assert json.loads(result.tool_results[0].content)["status"] == "audit_failed"


@pytest.mark.parametrize(
    ("text", "attachments", "has_quote", "expected"),
    [
        (" 同意 ", None, False, True),
        ("\t继续\n", None, False, True),
        (" 拒绝 ", None, False, True),
        ("\t忽略\n", None, False, True),
        (" APPROVE ", None, False, True),
        ("\tCoNtInUe\n", None, False, True),
        (" ReJeCt ", None, False, True),
        ("\tIGNORE\n", None, False, True),
        ("同意执行", None, False, False),
        ("忽略并放行", None, False, False),
        ("approve continue", None, False, False),
        ("", None, False, False),
        (None, None, False, False),
        ([{"type": "text", "text": "同意"}], None, False, False),
        ("同意", ["file.txt"], False, False),
        ("同意", None, True, False),
    ],
)
def test_confirmation_candidate_requires_one_exact_unquoted_text_word(text, attachments, has_quote, expected):
    assert is_confirmation_candidate(text, attachments=attachments, has_quote=has_quote) is expected


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
        ("忽略", None),
        ("", None),
    ],
)
def test_confirmation_words_are_strict(text, expected):
    assert parse_confirmation_decision(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("忽略", ConfirmationDecision.IGNORE),
        ("IGNORE", ConfirmationDecision.IGNORE),
        ("同意", None),
        ("拒绝", ConfirmationDecision.REJECT),
    ],
)
def test_high_risk_confirmation_words_are_strict(text, expected):
    assert parse_confirmation_decision(text, requires_high_risk_override=True) == expected


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
                {"tool_call_id": "call-1", "score": 1, "reason": "safe", "deny": False, "file_checks": []},
                {"tool_call_id": "call-2", "score": 5, "reason": "confirm", "deny": False, "file_checks": []},
            ]
        },
        ["call-1", "call-2"],
    )
    assert [item["tool_call_id"] for item in parsed] == ["call-1", "call-2"]
    assert [item["deny"] for item in parsed] == [False, False]

    with pytest.raises(ValueError):
        _parse_results(
            {
                "results": [
                    {"tool_call_id": "call-1", "score": 1, "reason": "safe", "deny": False, "file_checks": []},
                    {"tool_call_id": "call-1", "score": 1, "reason": "safe", "deny": False, "file_checks": []},
                ]
            },
            ["call-1", "call-2"],
        )

    with pytest.raises(ValueError):
        _parse_results(
            {"results": [{"tool_call_id": "call-1", "score": 1, "reason": "safe", "deny": False}]},
            ["call-1"],
        )


@pytest.mark.parametrize(
    ("deny_value", "include_deny"),
    [
        (None, False),
        (None, True),
        (0, True),
        (1, True),
        ("false", True),
        ([], True),
        ({}, True),
    ],
)
def test_batch_result_parser_rejects_missing_or_non_boolean_deny(deny_value, include_deny):
    result = {"tool_call_id": "call-1", "score": 4, "reason": "review", "file_checks": []}
    if include_deny:
        result["deny"] = deny_value

    with pytest.raises(ValueError):
        _parse_results({"results": [result]}, ["call-1"])


@pytest.mark.parametrize("deny", [False, True])
def test_batch_result_parser_preserves_boolean_deny_without_changing_score(deny):
    parsed = _parse_results(
        {"results": [{"tool_call_id": "call-1", "score": 6, "reason": "review", "deny": deny, "file_checks": []}]},
        ["call-1"],
    )

    assert parsed[0]["deny"] is deny
    assert parsed[0]["score"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_score", "audit_threshold", "expected_status"),
    [(1, 5, "passed"), (7, 5, "pending"), (8, 5, "pending")],
)
async def test_service_does_not_classify_dynamic_script_command_or_change_model_score(monkeypatch, tmp_path, model_score, audit_threshold, expected_status):
    import app.core.audit.service as service

    cfg = _profile_config()
    cfg.security.audit_threshold = audit_threshold
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
        return _auditor_response_context([{"tool_call_id": "call-1", "score": model_score, "reason": "model assessed command", "deny": False, "file_checks": []}])

    async def summarize_pending(*_args, **_kwargs):
        return "Model-selected confirmation summary", {}

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "_summarize_pending", summarize_pending)
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
        execution_scope="foreground",
    )

    assert result.status.value == expected_status
    assert (result.confirmation_payload is not None) is (expected_status == "pending")
    assert captured["request_payload"]["working_directory"] == str(tmp_path.resolve())
    assert captured["request_payload"]["tool_calls"][0]["arguments"] == {"command": command}
    assert set(captured["request_payload"]["tool_calls"][0]) == {"tool_call_id", "turn_index", "tool_name", "arguments"}
    assert captured["tool_details"][0]["file_snapshots"] == []
    assert captured["tool_details"][0]["score"] == model_score
    assert captured["tool_details"][0]["conclusion"] == expected_status
    if expected_status == "pending":
        expected_mode = "high_risk_override" if model_score == 8 else "standard"
        summary = "Model-selected confirmation summary"
        expires_at = captured["expires_at"].isoformat()
        timeout_seconds = cfg.security.audit_confirmation_timeout_seconds
        expected = {
            "type": "audit_confirmation",
            "audit_record_id": 123,
            "summary": summary,
            "risk": model_score,
            "status": "pending",
            "confirmation_mode": expected_mode,
            "expires_at": expires_at,
            "plain_text": t(
                MSG_AUDIT_HIGH_RISK_CONFIRMATION_IM if expected_mode == "high_risk_override" else MSG_AUDIT_CONFIRMATION_IM,
                locale="en",
                summary=summary,
                score=model_score,
                expires_in_seconds=timeout_seconds,
            ),
        }
        assert result.confirmation_payload == expected
        assert (
            audit_confirmation_payload(
                123,
                summary=summary,
                risk=model_score,
                confirmation_mode=expected_mode,
                expires_at=expires_at,
                timeout_seconds=timeout_seconds,
                locale="en",
            )
            == expected
        )
    if model_score == 8:
        plain_text = result.confirmation_payload["plain_text"].lower()
        assert "high-risk" in plain_text or "high risk" in plain_text or "高危" in plain_text
        assert "ignore" in plain_text or "忽略" in plain_text


@pytest.mark.asyncio
async def test_service_does_not_infer_missing_script_evidence_from_command_text(monkeypatch, tmp_path):
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
        return _auditor_response_context([{"tool_call_id": "call-1", "score": 0, "reason": "safe", "deny": False, "file_checks": []}])

    async def persist(*_args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(service, "cancel_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
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
        execution_scope="foreground",
    )

    assert result.status.value == "passed"
    assert result.confirmation_payload is None
    assert set(captured["request_payload"]["tool_calls"][0]) == {"tool_call_id", "turn_index", "tool_name", "arguments"}
    assert captured["tool_details"][0]["score"] == 0
    assert captured["tool_details"][0]["server_confirmation_reasons"] == []


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
        return _auditor_response_context([{"tool_call_id": "call-1", "score": 0, "reason": "read only", "deny": False, "file_checks": []}])

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
        execution_scope="foreground",
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
    cfg.tool.allowed_operation_dirs = [str(tmp_path)]
    captured = {}
    calls = []
    script = tmp_path / "test.py"
    script.write_text("from pathlib import Path\nPath('important.txt').unlink()\n", encoding="utf-8")
    task_instruction = (
        "Run the following scripts in order:\n1. python test.py\n2. bash verify.sh --strict\n3. pwsh cleanup.ps1 -WhatIf\nLimits:\n- Keep all operations under the configured working directory.\n- Do not delete or modify files outside that directory.\n- Stop after one pass and require confirmation before execution."
    )

    class FakeDb:
        async def commit(self):
            return None

    class FakeChannel:
        id = 1
        name = "audit-channel"
        usage_key = "audit-channel"
        is_active = True
        base_url = "https://audit.example"
        model_ids = [{"model_id": "audit-model", "usage": "CHAT", "protocol": "OPENAI"}]

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
        task_instruction=task_instruction,
    )

    system_prompt = calls[0]["messages"][0].content
    assert summary == "Run test.py, which deletes important.txt; execution is awaiting confirmation."
    assert len(calls) == 2
    assert all(call["model_id"] == cfg.security.audit_model_id for call in calls)
    assert calls[0]["tools"][0]["function"]["name"] == "read_text_file"
    assert all(call["max_tokens"] == DEFAULT_CHAT_MAX_TOKENS for call in calls)
    assert all(isinstance(call["request_context_tokens"], int) and call["request_context_tokens"] >= 0 for call in calls)
    tool_payload = json.loads(calls[1]["messages"][-1].content)
    assert tool_payload["content"] == script.read_bytes().decode("utf-8")
    assert system_prompt == AUDIT_SUMMARY_PROMPT.format(audit_report_language=cfg.security.audit_report_language)
    summary_payload = json.loads(calls[0]["messages"][1].content)
    assert summary_payload["task_instruction"] == task_instruction
    assert summary_payload["working_directory"] == str(tmp_path.resolve())
    assert summary_payload["tool_calls"] == [{"id": "call-1", "name": "execute_shell", "arguments": {"command": "python test.py"}}]
    assert summary_payload["server_confirmation_reasons"] == reasons
    assert summary_payload["audit_results"] == []
    assert summary_payload["audit_depth"] == cfg.security.audit_depth == 3
    assert summary_payload["remaining_file_reads"] == cfg.security.audit_depth
    assert summary_payload["directory_policy"] == {
        "write_protected_directories": [str(directory) for directory in get_write_protected_dirs()],
        "allowed_operation_dirs": cfg.tool.allowed_operation_dirs,
    }


def test_tool_round_precheck_fails_entire_round_before_execution():
    cfg = _profile_config()
    calls = [
        SimpleNamespace(id="call-1", name="execute_shell", arguments={}),
        SimpleNamespace(id="call-2", name="file_tool", arguments={"operation": "write", "path": "ok.txt", "content": "ok"}),
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
