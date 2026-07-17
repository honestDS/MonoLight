import asyncio
from types import SimpleNamespace

import pytest

from app.adapters.weixin_openclaw.response import extract_event_reply
from app.core.audit.confirmation import ConfirmationDecision, message_has_quote, parse_confirmation_decision
from app.core.audit.service import (
    AUDIT_FILE_MAX_BYTES,
    _apply_evidence_score_floor,
    _call_auditor,
    _collect_file_candidates,
    _parse_results,
    _read_candidate_sync,
    _requires_confirmation_from_evidence,
    _summarize_pending,
    classify_audit_score,
)
from app.core.message_platforms.inbound_collector import InboundMessageCollector
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


def test_direct_script_requires_complete_read_and_model_file_check(tmp_path):
    script = tmp_path / "entry.py"
    script.write_text("print('ok')", encoding="utf-8")
    tool_call = InternalToolCall(id="call-1", name="execute_shell", arguments={"command": 'python "entry.py"'})
    _request_candidates, snapshots_by_call, candidates_by_path = _collect_file_candidates([tool_call], tmp_path)
    snapshots = snapshots_by_call[tool_call.id]
    read_result = _read_candidate_sync(str(script.resolve()), candidates_by_path, {"bytes": 0, "calls": 0})

    assert _requires_confirmation_from_evidence(tool_call, snapshots, [], [{"path": "entry.py", "status": "ok"}])
    assert _requires_confirmation_from_evidence(tool_call, snapshots, [read_result], [])
    assert not _requires_confirmation_from_evidence(tool_call, snapshots, [read_result], [{"path": "entry.py", "status": "ok"}])


def test_audit_report_language_defaults_and_rejects_unsupported_locale():
    assert _profile_config().security.audit_report_language == "zh"

    with pytest.raises(ValueError):
        ProfileConfig.model_validate({"security": {"audit_report_language": "unsupported"}})


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


def test_audit_file_reader_only_reads_approved_direct_candidate(tmp_path):
    script = tmp_path / "entry.py"
    script.write_text("print('ok')", encoding="utf-8")
    tool_call = InternalToolCall(
        id="call-1",
        name="execute_shell",
        arguments={"command": f'python "{script}"'},
    )
    request_candidates, snapshots, candidates_by_path = _collect_file_candidates([tool_call], tmp_path)

    assert request_candidates["call-1"][0]["resolved_path"] == str(script.resolve())
    assert snapshots["call-1"][0]["sha256"]
    read_state = {"bytes": 0, "calls": 0}
    approved = _read_candidate_sync(str(script.resolve()), candidates_by_path, read_state)
    denied = _read_candidate_sync(str(tmp_path / "other.py"), candidates_by_path, read_state)

    assert approved["content"] == "print('ok')"
    assert denied["status"] == "denied"


def test_audit_file_reader_marks_large_file_truncated(tmp_path):
    script = tmp_path / "large.py"
    script.write_text("x" * (AUDIT_FILE_MAX_BYTES + 1), encoding="utf-8")
    tool_call = InternalToolCall(
        id="call-1",
        name="execute_shell",
        arguments={"command": f'python "{script}"'},
    )
    _request_candidates, snapshots, candidates_by_path = _collect_file_candidates([tool_call], tmp_path)
    result = _read_candidate_sync(str(script.resolve()), candidates_by_path, {"bytes": 0, "calls": 0})

    assert snapshots["call-1"][0]["truncated"] is True
    assert result["truncated"] is True
    assert len(result["content"]) == AUDIT_FILE_MAX_BYTES


@pytest.mark.asyncio
async def test_auditor_can_only_read_approved_file_through_restricted_tool(tmp_path, monkeypatch):
    script = tmp_path / "entry.py"
    script.write_text("print('ok')", encoding="utf-8")
    tool_call = InternalToolCall(
        id="call-1",
        name="execute_shell",
        arguments={"command": f'python "{script}"'},
    )
    request_candidates, _snapshots, candidates_by_path = _collect_file_candidates([tool_call], tmp_path)
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
                            name="read_audit_file",
                            arguments={"path": str(script.resolve())},
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
            "tool_calls": [
                {
                    "tool_call_id": "call-1",
                    "direct_file_candidates": request_candidates["call-1"],
                }
            ],
        },
        candidates_by_path,
    )

    assert len(calls) == 2
    assert calls[0]["tools"][0]["function"]["name"] == "read_audit_file"
    assert all(tool["function"]["name"] == "read_audit_file" for tool in calls[1]["tools"])
    assert response_context["file_reads"][0]["content"] == "print('ok')"
    assert any(message["role"] == "tool" for message in request_context["messages"])


@pytest.mark.asyncio
async def test_pending_summary_prompt_uses_configured_report_language(monkeypatch):
    cfg = _profile_config()
    cfg.security.audit_channel_id = 1
    cfg.security.audit_model_id = "audit-model"
    cfg.security.audit_report_language = "en"
    captured = {}

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
        captured.update(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="Run the requested command."),
            model="audit-model",
        )

    monkeypatch.setattr("app.core.audit.service.channel_crud.get", get_channel)
    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)

    summary, _context = await _summarize_pending(
        FakeDb(),
        cfg,
        [{"id": "call-1", "name": "execute_shell", "arguments": {"command": "echo ok"}}],
    )

    system_prompt = captured["messages"][0].content
    assert summary == "Run the requested command."
    assert "locale code: en" in system_prompt
    assert "entire sentence only in that language" in system_prompt


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
