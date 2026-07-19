import asyncio
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.adapters.weixin_openclaw.response import extract_event_reply
from app.core.audit.confirmation import ConfirmationDecision, message_has_quote, parse_confirmation_decision
from app.core.audit.service import (
    AUDIT_DIRECT_SCRIPT_MAX_CANDIDATES,
    AUDIT_FILE_MAX_BYTES,
    _apply_evidence_score_floor,
    _call_auditor,
    _collect_file_candidates,
    _direct_script_paths,
    _file_checks_are_sufficient,
    _parse_results,
    _read_candidate_sync,
    _requires_confirmation_from_evidence,
    _summarize_pending,
    audit_tool_round,
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
    _request_candidates, snapshots_by_call, candidates_by_path, _reasons = _collect_file_candidates([tool_call], tmp_path)
    snapshots = snapshots_by_call[tool_call.id]
    read_result = _read_candidate_sync(str(script.resolve()), candidates_by_path, {"bytes": 0, "calls": 0})

    assert _requires_confirmation_from_evidence(tool_call, snapshots, [], [{"path": "entry.py", "status": "ok"}])
    assert _requires_confirmation_from_evidence(tool_call, snapshots, [read_result], [])
    assert not _requires_confirmation_from_evidence(tool_call, snapshots, [read_result], [{"path": "entry.py", "status": "ok"}])


def test_direct_script_file_checks_require_structured_matching_server_evidence(tmp_path):
    script = tmp_path / "entry.py"
    script.write_text("print('ok')", encoding="utf-8")
    tool_call = InternalToolCall(id="call-1", name="execute_shell", arguments={"command": 'python "entry.py"'})
    _request_candidates, snapshots_by_call, candidates_by_path, _reasons = _collect_file_candidates([tool_call], tmp_path)
    snapshots = snapshots_by_call[tool_call.id]
    read_result = _read_candidate_sync(str(script.resolve()), candidates_by_path, {"bytes": 0, "calls": 0})
    valid_check = {
        "path": "entry.py",
        "status": "ok",
        "sha256": snapshots[0]["sha256"],
        "size": snapshots[0]["size"],
    }

    assert _file_checks_are_sufficient(snapshots, [read_result], [valid_check])
    assert not _file_checks_are_sufficient(snapshots, [read_result], ["not a check"])
    assert not _file_checks_are_sufficient(snapshots, [read_result], [{**valid_check, "path": "other.py"}])
    assert not _file_checks_are_sufficient(snapshots, [read_result], [{**valid_check, "status": "failed"}])
    assert not _file_checks_are_sufficient(snapshots, [read_result], [{**valid_check, "sha256": "0" * 64}])

    read_result["truncated"] = True
    assert not _file_checks_are_sufficient(snapshots, [read_result], [valid_check])


def test_direct_script_extraction_preserves_all_candidates_and_dynamic_metadata():
    command = "python " + " ".join(f"script-{index}.py" for index in range(11))
    extraction = _direct_script_paths(command)

    assert extraction.unique_candidate_count == 11
    assert len(extraction.selected_candidates) == AUDIT_DIRECT_SCRIPT_MAX_CANDIDATES
    assert extraction.excess_candidate_count == 1
    assert extraction.parse_failure is None
    assert extraction.dynamic_interpreter_targets == ()

    dynamic = _direct_script_paths('python "$SCRIPT"')
    assert dynamic.dynamic_interpreter_targets == ("$SCRIPT",)
    assert _direct_script_paths('echo "$SCRIPT"').dynamic_interpreter_targets == ()

    invalid = _direct_script_paths('python "unterminated.py')
    assert invalid.parse_failure


@pytest.mark.parametrize(
    ("command", "expected_targets"),
    [
        ('env python "$SCRIPT"', ("$SCRIPT",)),
        ('env -i --unset=PYTHONPATH python "$SCRIPT"', ("$SCRIPT",)),
        ('env -S "python $SCRIPT"', ("$SCRIPT",)),
        ('sudo -u runner --preserve-env python "$SCRIPT"', ("$SCRIPT",)),
        ('sudo -H python "$SCRIPT"', ("$SCRIPT",)),
        ("cmd /d /s /c python %SCRIPT%", ("%SCRIPT%",)),
        ('cmd /c "python %SCRIPT%"', ("%SCRIPT%",)),
        ('env powershell -File "$SCRIPT"', ("$SCRIPT",)),
        ('sudo pwsh -Command "$SCRIPT"', ("pwsh -Command",)),
        ("cmd /c pwsh -File %SCRIPT%", ("%SCRIPT%",)),
    ],
)
def test_dynamic_interpreter_targets_cover_wrappers_without_shell_expansion(tmp_path, command, expected_targets):
    """验证跨平台包装器中的动态解释器目标进入服务端确认原因。"""
    extraction = _direct_script_paths(command)
    assert extraction.dynamic_interpreter_targets == expected_targets

    tool_call = InternalToolCall(id="call-1", name="execute_shell", arguments={"command": command})
    _request_candidates, _snapshots, _candidates_by_path, reasons = _collect_file_candidates([tool_call], tmp_path)
    assert reasons[tool_call.id][0]["code"] == "dynamic_interpreter_target"

    assert _direct_script_paths("echo $SCRIPT").dynamic_interpreter_targets == ()
    assert _direct_script_paths("cmd /c echo %SCRIPT%").dynamic_interpreter_targets == ()


def test_audit_report_language_defaults_and_rejects_unsupported_locale():
    assert _profile_config().security.audit_report_language == "zh"
    assert _profile_config().security.audit_confirmation_timeout_minutes == 10

    with pytest.raises(ValueError):
        ProfileConfig.model_validate({"security": {"audit_report_language": "unsupported"}})


@pytest.mark.parametrize("timeout", [1, 1440])
def test_audit_confirmation_timeout_accepts_configured_boundaries(timeout):
    cfg = ProfileConfig.model_validate({"security": {"audit_confirmation_timeout_minutes": timeout}})

    assert cfg.security.audit_confirmation_timeout_minutes == timeout


@pytest.mark.parametrize("timeout", [0, 1441, 1.5, "10", True])
def test_audit_confirmation_timeout_rejects_invalid_values(timeout):
    with pytest.raises(ValueError):
        ProfileConfig.model_validate({"security": {"audit_confirmation_timeout_minutes": timeout}})


@pytest.mark.asyncio
async def test_pending_audit_uses_configured_confirmation_timeout(monkeypatch):
    import app.core.audit.service as service

    cfg = _profile_config()
    cfg.security.audit_confirmation_timeout_minutes = 25
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

    monkeypatch.setattr(service, "expire_confirmation_by_session", no_expiration)
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
    assert captured["expires_at"] == fixed_now + timedelta(minutes=25)


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

    monkeypatch.setattr(service, "expire_confirmation_by_session", no_expiration)
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
    request_candidates, snapshots, candidates_by_path, _reasons = _collect_file_candidates([tool_call], tmp_path)

    assert request_candidates["call-1"][0]["resolved_path"] == str(script.resolve())
    assert snapshots["call-1"][0]["sha256"]
    read_state = {"bytes": 0, "calls": 0}
    approved = _read_candidate_sync(str(script.resolve()), candidates_by_path, read_state)
    denied = _read_candidate_sync(str(tmp_path / "other.py"), candidates_by_path, read_state)

    assert approved["content"] == "print('ok')"
    assert denied["status"] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "score", "threshold", "expected_status"),
    [
        ("eleven", 0, 5, "pending"),
        ("eleven", 0, 0, "pending"),
        ("dynamic", 0, 0, "pending"),
        ("echo", 0, 0, "passed"),
        ("eleven", 8, 0, "blocked"),
    ],
)
async def test_server_confirmation_reasons_control_round_status_and_persistence(monkeypatch, tmp_path, case, score, threshold, expected_status):
    import app.core.audit.service as service

    commands = {
        "eleven": "python " + " ".join(f"script-{index}.py" for index in range(11)),
        "dynamic": 'python "$SCRIPT"',
        "echo": 'echo "$SCRIPT"',
    }
    cfg = _profile_config()
    cfg.security.audit_threshold = threshold
    captured = {}

    class FakeDb:
        async def commit(self):
            return None

    async def no_expiration(*_args, **_kwargs):
        return None

    async def create_preparing(*_args, **_kwargs):
        return SimpleNamespace(id=123)

    async def call_auditor(*_args, **_kwargs):
        captured["request_payload"] = _args[2]
        return {"messages": []}, {
            "parsed": {
                "results": [
                    {
                        "tool_call_id": "call-1",
                        "score": score,
                        "reason": "model reason",
                        "file_checks": [],
                    }
                ]
            }
        }

    async def summarize_pending(*args, **_kwargs):
        captured["summary_reasons"] = args[3]
        return "Confirm command", {}

    async def persist(*_args, **kwargs):
        captured["context_payload"] = kwargs["context_payload"]
        return True

    monkeypatch.setattr(service, "expire_confirmation_by_session", no_expiration)
    monkeypatch.setattr(service.audit_crud, "create_preparing", create_preparing)
    monkeypatch.setattr(service, "_call_auditor", call_auditor)
    monkeypatch.setattr(service, "_summarize_pending", summarize_pending)
    monkeypatch.setattr(service, "persist_prepared_audit_round", persist)

    result = await service.audit_tool_round(
        FakeDb(),
        cfg=cfg,
        tool_calls=[InternalToolCall(id="call-1", name="execute_shell", arguments={"command": commands[case]})],
        source_assistant_message_id=1,
        uid="u1",
        operator_username="tester",
        session_id="session-1",
        source="web",
        language="en",
        working_directory=tmp_path,
    )

    assert result.status.value == expected_status
    request_tool = captured["request_payload"]["tool_calls"][0]
    metadata = request_tool["direct_file_candidate_metadata"]
    if case == "eleven":
        assert metadata["unique_candidate_count"] == 11
        assert len(request_tool["direct_file_candidates"]) == 10
        assert len(captured["context_payload"]["results"][0]["file_snapshots"]) == 10
        assert captured["context_payload"]["server_confirmation_reasons"]["call-1"]
        if expected_status == "pending":
            assert captured["summary_reasons"]["call-1"]
    elif case == "dynamic":
        assert metadata["dynamic_interpreter_targets"] == ["$SCRIPT"]
        assert captured["context_payload"]["server_confirmation_reasons"]["call-1"][0]["code"] == "dynamic_interpreter_target"
    else:
        assert metadata["dynamic_interpreter_targets"] == []
        assert captured["context_payload"]["server_confirmation_reasons"] == {}

    if expected_status != "passed":
        tool_message = json.loads(result.tool_results[0].content)
        assert "model reason" in tool_message["reason"]


def test_append_file_candidate_records_missing_target_and_ignores_non_append_or_outside_paths(tmp_path):
    calls = [
        InternalToolCall(id="append", name="write_file", arguments={"file_path": "nested/new.txt", "content": "new", "append": True}),
        InternalToolCall(id="overwrite", name="write_file", arguments={"file_path": "overwrite.txt", "content": "new", "append": False}),
        InternalToolCall(id="outside", name="write_file", arguments={"file_path": "../outside.txt", "content": "new", "append": True}),
    ]

    request_candidates, snapshots, _candidates_by_path, _reasons = _collect_file_candidates(calls, tmp_path)

    snapshot = snapshots["append"][0]
    assert snapshot["absolute_path"] == str((tmp_path / "nested" / "new.txt").resolve())
    assert snapshot["exists"] is False
    assert snapshot["status"] == "missing"
    assert snapshot["size"] is None
    assert snapshot["sha256"] is None
    assert "overwrite" not in snapshots
    assert "outside" not in snapshots
    assert request_candidates["append"][0]["absolute_path"] == snapshot["absolute_path"]


def test_append_file_candidate_records_missing_target_before_workspace_exists(tmp_path):
    workspace = tmp_path / "temp_new_user"
    tool_call = InternalToolCall(id="append", name="write_file", arguments={"file_path": "nested/new.txt", "content": "new", "append": True})

    _request_candidates, snapshots, _candidates_by_path, _reasons = _collect_file_candidates([tool_call], workspace)

    snapshot = snapshots["append"][0]
    assert workspace.exists() is False
    assert snapshot["absolute_path"] == str(workspace / "nested" / "new.txt")
    assert snapshot["exists"] is False
    assert snapshot["file_type"] == "missing"


def test_append_file_candidate_preserves_link_type_and_resolved_path(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("content", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        os.symlink(target, link)
    except OSError as exc:
        pytest.skip(f"当前系统不允许创建测试链接: {exc}")

    tool_call = InternalToolCall(id="call-1", name="write_file", arguments={"file_path": "link.txt", "content": "append", "append": True})
    _request_candidates, snapshots, _candidates_by_path, _reasons = _collect_file_candidates([tool_call], tmp_path)

    snapshot = snapshots[tool_call.id][0]
    assert snapshot["absolute_path"] == str(link)
    assert snapshot["resolved_path"] == str(target.resolve())
    assert snapshot["exists"] is True
    assert snapshot["file_type"] == "symlink"
    assert snapshot["status"] == "ok"


def test_audit_file_reader_marks_large_file_truncated(tmp_path):
    script = tmp_path / "large.py"
    script.write_text("x" * (AUDIT_FILE_MAX_BYTES + 1), encoding="utf-8")
    tool_call = InternalToolCall(
        id="call-1",
        name="execute_shell",
        arguments={"command": f'python "{script}"'},
    )
    _request_candidates, snapshots, candidates_by_path, _reasons = _collect_file_candidates([tool_call], tmp_path)
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
    request_candidates, _snapshots, candidates_by_path, _reasons = _collect_file_candidates([tool_call], tmp_path)
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

    reasons = {
        "call-1": [
            {
                "code": "direct_script_candidates_exceeded",
                "message": "The number of directly referenced scripts (11) exceeds the inspection limit (10)",
                "details": {"candidate_count": 11, "limit": 10, "excess_count": 1},
            }
        ]
    }
    summary, _context = await _summarize_pending(
        FakeDb(),
        cfg,
        [{"id": "call-1", "name": "execute_shell", "arguments": {"command": "echo ok"}}],
        reasons,
    )

    system_prompt = captured["messages"][0].content
    assert summary == "Run the requested command."
    assert "locale code: en" in system_prompt
    assert "entire sentence only in that language" in system_prompt
    assert json.loads(captured["messages"][1].content) == {
        "tool_calls": [{"id": "call-1", "name": "execute_shell", "arguments": {"command": "echo ok"}}],
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
