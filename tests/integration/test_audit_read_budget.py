import json
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.core.constants import MSG_AUDIT_WAITING_CONFIRMATION, READ_TEXT_FILE_TOOL_NAME, SHELL_TOOL_NAME
from app.core.crud.audit.audit import audit_crud
from app.core.i18n import t
from app.core.tools.shell import ShellExecutor
from app.models.audit import AuditExecutionRecord, AuditFailureType, AuditRecord, AuditRecordStatus, AuditToolConclusion
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole
from tests.integration.test_audit_directory_policy import (
    AUDIT_MODEL_ID,
    _audit_config,
    _audit_response,
    _audit_round,
    _patch_audit_root,
    _seed_audit_channel,
)

_FILE_CHECK_FIELDS = (
    "original_path",
    "absolute_path",
    "resolved_path",
    "exists",
    "file_type",
    "status",
    "size",
    "sha256",
    "truncated",
    "bytes_read",
)


@pytest.fixture(autouse=True)
def isolate_audit_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
async def audit_database(database_factory):
    async with database_factory(name="audit-read-budget.sqlite3") as session_factory:
        yield session_factory


def _read_call(call_id: str, tool_call_id: str, path: Path) -> InternalToolCall:
    return InternalToolCall(
        id=call_id,
        name=READ_TEXT_FILE_TOOL_NAME,
        arguments={"path": str(path), "tool_call_id": tool_call_id},
    )


def _shell_call(call_id: str, tool_call_id: str, command: str) -> InternalToolCall:
    return InternalToolCall(
        id=call_id,
        name=SHELL_TOOL_NAME,
        arguments={"command": command, "execution_mode": "non_interactive", "tool_call_id": tool_call_id},
    )


def _environment_query_command() -> str:
    if os.name == "nt":
        return "echo %MONOLIGHT_AUDIT_TEST_PATH% & cd"
    return "printf '%s\\n' \"$MONOLIGHT_AUDIT_TEST_PATH\"; pwd"


def _record_real_shell_execution(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, str]]) -> None:
    real_execute = ShellExecutor.execute

    async def recording_execute(executor: ShellExecutor, command: str, execution_mode: str) -> str:
        calls.append((command, execution_mode))
        return await real_execute(executor, command, execution_mode)

    monkeypatch.setattr(ShellExecutor, "execute", recording_execute)


def _tool_response(tool_calls: list[InternalToolCall]) -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(role=MessageRole.ASSISTANT, tool_calls=tool_calls),
        model=AUDIT_MODEL_ID,
        finish_reason="tool_calls",
    )


def _text_response(content: str) -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
        model=AUDIT_MODEL_ID,
        finish_reason="stop",
    )


def _score_response(tool_call_id: str, file_checks: list[dict[str, Any]]) -> InternalResponse:
    response = _audit_response([tool_call_id], deny=False, score=5)
    payload = json.loads(response.message.content or "{}")
    payload["results"][0]["file_checks"] = file_checks
    response.message.content = json.dumps(payload, ensure_ascii=False)
    return response


def _model_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    return json.loads(kwargs["messages"][1].content)


def _tool_results(messages: list[InternalMessage]) -> list[dict[str, Any]]:
    return [json.loads(message.content) for message in messages if message.role == MessageRole.TOOL and isinstance(message.content, str)]


def _file_check(read_result: dict[str, Any]) -> dict[str, Any]:
    return {field: read_result[field] for field in _FILE_CHECK_FIELDS}


@pytest.mark.asyncio
async def test_scoring_shell_evidence_shares_budget_with_file_reads(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")

    evidence_file = tmp_path / "evidence.txt"
    evidence_file.write_text("small verified evidence", encoding="utf-8")
    working_directory = tmp_path / "shell-working"
    working_directory.mkdir()
    environment_path = tmp_path / "shell-environment"
    monkeypatch.setenv("MONOLIGHT_AUDIT_TEST_PATH", str(environment_path))
    assert all(not any(char in str(path) for char in "&|;<>\"'`$%()!") for path in (tmp_path, evidence_file, working_directory, environment_path))

    audited_call = InternalToolCall(
        id="scoring-shell-audited-command",
        name=SHELL_TOOL_NAME,
        arguments={"command": "echo BUSINESS_COMMAND_MUST_NOT_RUN", "execution_mode": "non_interactive"},
    )
    query_command = _environment_query_command()
    overflow_command = "echo AUDIT_OVER_BUDGET_MARKER"
    cfg = _audit_config(tmp_path, threshold=5)
    cfg.security.audit_depth = 2
    generate_calls: list[dict[str, Any]] = []
    shell_executions: list[tuple[str, str]] = []
    _record_real_shell_execution(monkeypatch, shell_executions)

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        if len(generate_calls) == 1:
            payload = _model_payload(kwargs)
            assert payload["remaining_evidence_calls"] == 2
            assert payload["remaining_shell_calls"] == 2
            assert kwargs["tools"] is not None
            return _tool_response(
                [
                    _shell_call("scoring-shell-query", audited_call.id, query_command),
                    _read_call("scoring-file-read", audited_call.id, evidence_file),
                    _shell_call("scoring-shell-overflow", audited_call.id, overflow_command),
                ]
            )
        if len(generate_calls) == 2:
            assert kwargs["tools"] is None
            results = _tool_results(kwargs["messages"])
            successful_read = next(item for item in results if item.get("absolute_path") == str(evidence_file))
            return _score_response(audited_call.id, [_file_check(successful_read)])
        assert len(generate_calls) == 3
        payload = _model_payload(kwargs)
        assert payload["remaining_evidence_calls"] == 0
        assert payload["remaining_shell_calls"] == 0
        assert kwargs["tools"] is None
        return _text_response("仅文字摘要，不再请求工具。")

    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)
    async with audit_database() as db:
        result = await _audit_round(db, cfg=cfg, tool_calls=[audited_call], working_directory=working_directory)

    assert result is not None
    assert result.status is AuditRecordStatus.PENDING
    assert result.may_execute is False
    assert len(generate_calls) == 3
    assert shell_executions == [(query_command, "non_interactive")]
    scoring_results = _tool_results(generate_calls[1]["messages"])
    assert [item["status"] for item in scoring_results] == ["ok", "ok", "limit_exceeded"]
    assert json.loads(scoring_results[0]["content"])["stdout"].count(str(environment_path)) == 1
    assert str(working_directory) in json.loads(scoring_results[0]["content"])["stdout"]
    assert "BUSINESS_COMMAND_MUST_NOT_RUN" not in json.dumps(scoring_results)

    async with audit_database() as db:
        records = (await db.scalars(select(AuditRecord))).all()
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        assert record is not None
        assert record.context_file_path is not None
        context = json.loads(Path(record.context_file_path).read_text(encoding="utf-8"))

    assert len(records) == 1
    assert records[0].id == result.audit_record_id
    assert context["file_read_calls"] == 1
    assert context["shell_calls"] == 1
    assert context["evidence_calls"] == 2
    scoring_response = context["scoring_response"]
    assert len(scoring_response["file_reads"]) == 1
    assert [item["status"] for item in scoring_response["shell_results"]] == ["ok", "limit_exceeded"]
    assert scoring_response["parsed"]["results"][0]["file_checks"] == [_file_check(scoring_response["file_reads"][0])]
    assert context["summary"]["response"] == "仅文字摘要，不再请求工具。"
    assert context["summary"]["shell_results"] == []
    assert len(details) == 1
    assert len(details[0].file_snapshots) == 1
    assert details[0].file_snapshots[0]["absolute_path"] == str(evidence_file)
    assert details[0].file_snapshots[0]["content"] == "small verified evidence"
    assert "command" not in details[0].file_snapshots[0]


@pytest.mark.asyncio
async def test_summary_shell_evidence_uses_only_remaining_shared_budget(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")

    evidence_file = tmp_path / "summary-evidence.txt"
    evidence_file.write_text("small summary evidence", encoding="utf-8")
    working_directory = tmp_path / "summary-shell-working"
    working_directory.mkdir()
    environment_path = tmp_path / "summary-shell-environment"
    monkeypatch.setenv("MONOLIGHT_AUDIT_TEST_PATH", str(environment_path))
    assert all(not any(char in str(path) for char in "&|;<>\"'`$%()!") for path in (tmp_path, evidence_file, working_directory, environment_path))

    audited_call = InternalToolCall(
        id="summary-shell-audited-command",
        name=SHELL_TOOL_NAME,
        arguments={"command": "echo BUSINESS_COMMAND_MUST_NOT_RUN", "execution_mode": "non_interactive"},
    )
    query_command = _environment_query_command()
    cfg = _audit_config(tmp_path, threshold=5)
    cfg.security.audit_depth = 2
    generate_calls: list[dict[str, Any]] = []
    shell_executions: list[tuple[str, str]] = []
    _record_real_shell_execution(monkeypatch, shell_executions)

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        if len(generate_calls) == 1:
            payload = _model_payload(kwargs)
            assert payload["remaining_evidence_calls"] == 2
            return _tool_response([_read_call("scoring-file-read", audited_call.id, evidence_file)])
        if len(generate_calls) == 2:
            assert kwargs["tools"] is not None
            successful_read = next(item for item in _tool_results(kwargs["messages"]) if item.get("absolute_path") == str(evidence_file))
            return _score_response(audited_call.id, [_file_check(successful_read)])
        if len(generate_calls) == 3:
            payload = _model_payload(kwargs)
            assert payload["remaining_evidence_calls"] == 1
            assert payload["remaining_shell_calls"] == 1
            assert kwargs["tools"] is not None
            return _tool_response(
                [
                    _shell_call("summary-shell-query", audited_call.id, query_command),
                    _read_call("summary-overflow-read", audited_call.id, evidence_file),
                ]
            )
        assert len(generate_calls) == 4
        assert kwargs["tools"] is None
        results = _tool_results(kwargs["messages"])
        assert [item["status"] for item in results] == ["ok", "limit_exceeded"]
        return _text_response("摘要中的shell证据真实执行，额外读取超出共享额度。")

    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)
    async with audit_database() as db:
        result = await _audit_round(db, cfg=cfg, tool_calls=[audited_call], working_directory=working_directory)

    assert result is not None
    assert result.status is AuditRecordStatus.PENDING
    assert result.may_execute is False
    assert len(generate_calls) == 4
    assert shell_executions == [(query_command, "non_interactive")]
    summary_results = _tool_results(generate_calls[3]["messages"])
    assert json.loads(summary_results[0]["content"])["stdout"].count(str(environment_path)) == 1
    assert str(working_directory) in json.loads(summary_results[0]["content"])["stdout"]
    assert "BUSINESS_COMMAND_MUST_NOT_RUN" not in json.dumps(summary_results)

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        assert record is not None
        assert record.context_file_path is not None
        context = json.loads(Path(record.context_file_path).read_text(encoding="utf-8"))

    assert context["file_read_calls"] == 1
    assert context["shell_calls"] == 1
    assert context["evidence_calls"] == 2
    assert len(context["scoring_response"]["file_reads"]) == 1
    assert context["summary"]["file_reads"][0]["status"] == "limit_exceeded"
    assert context["summary"]["file_reads"][0]["remaining_evidence_calls"] == 0
    assert len(context["summary"]["shell_results"]) == 1
    shell_result = context["summary"]["shell_results"][0]
    assert shell_result["status"] == "ok"
    assert shell_result["remaining_evidence_calls"] == 0
    assert str(environment_path) in json.loads(shell_result["content"])["stdout"]
    assert str(working_directory) in json.loads(shell_result["content"])["stdout"]
    assert len(details) == 1
    assert len(details[0].file_snapshots) == 1
    assert details[0].file_snapshots[0]["absolute_path"] == str(evidence_file)


@pytest.mark.parametrize("depth", [1, 3, 5])
@pytest.mark.asyncio
async def test_scoring_batch_and_summary_share_read_budget(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    depth: int,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    audit_root = tmp_path / "audit"
    _patch_audit_root(monkeypatch, audit_root)

    evidence_file = tmp_path / "evidence.txt"
    evidence_file.write_text("small verified evidence", encoding="utf-8")
    missing_file = tmp_path / "missing-evidence.txt"
    audited_call = InternalToolCall(
        id="read-budget-shell-call",
        name="execute_shell",
        arguments={"command": f'python "{evidence_file}"', "execution_mode": "non_interactive"},
    )
    read_calls = [_read_call("score-read-0", audited_call.id, evidence_file)]
    if depth >= 3:
        read_calls.extend(
            [
                _read_call("score-read-1", audited_call.id, evidence_file),
                _read_call("score-read-2", audited_call.id, missing_file),
            ]
        )
    while len(read_calls) < depth + 1:
        overflow_path = missing_file if depth == 1 else evidence_file
        read_calls.append(_read_call(f"score-read-{len(read_calls)}", audited_call.id, overflow_path))

    cfg = _audit_config(tmp_path, threshold=5)
    cfg.security.audit_depth = depth
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        if len(generate_calls) == 1:
            payload = _model_payload(kwargs)
            assert payload["audit_depth"] == depth
            assert payload["remaining_file_reads"] == depth
            assert kwargs["tools"] is not None
            return _tool_response(read_calls)
        if len(generate_calls) == 2:
            assert kwargs["tools"] is None
            results = _tool_results(kwargs["messages"])
            successful_read = next(item for item in results if item["status"] == "ok")
            return _score_response(audited_call.id, [_file_check(successful_read)])
        if len(generate_calls) == 3:
            payload = _model_payload(kwargs)
            assert payload["remaining_file_reads"] == 0
            assert payload["audit_results"][0]["score"] == 5
            assert payload["audit_results"][0]["deny"] is False
            assert kwargs["tools"] is None
            return _text_response("确认后将执行该命令。")
        raise AssertionError("audit read budget should stop the model loop")

    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)
    async with audit_database() as db:
        result = await _audit_round(db, cfg=cfg, tool_calls=[audited_call], working_directory=tmp_path)

    assert result is not None
    assert result.status is AuditRecordStatus.PENDING
    assert result.may_execute is False
    assert len(generate_calls) == 3
    scoring_results = _tool_results(generate_calls[1]["messages"])
    assert len(scoring_results) == depth + 1
    assert scoring_results[-1]["status"] == "limit_exceeded"
    assert "content" not in scoring_results[-1]
    assert all(item["tool_call_id"] == audited_call.id for item in scoring_results)
    if depth >= 3:
        assert sum(item["status"] == "missing" for item in scoring_results) == 1
        assert sum(item["status"] == "ok" for item in scoring_results) == depth - 1
        assert scoring_results[0]["absolute_path"] == scoring_results[1]["absolute_path"]

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        assert record is not None
        assert record.status is AuditRecordStatus.PENDING
        assert record.context_file_path is not None
        context_path = Path(record.context_file_path)
        assert context_path.is_file()
        context = json.loads(context_path.read_text(encoding="utf-8"))

    assert context["audit_depth"] == depth
    assert context["file_read_calls"] == depth
    assert len(context["scoring_response"]["file_reads"]) == depth + 1
    assert context["scoring_response"]["file_reads"][-1]["status"] == "limit_exceeded"
    assert context["summary"]["response"] == "确认后将执行该命令。"
    assert len(details) == 1
    assert details[0].conclusion is AuditToolConclusion.PENDING
    assert details[0].score == 5
    snapshots = details[0].file_snapshots
    assert [snapshot["status"] for snapshot in snapshots] == (["ok"] if depth == 1 else ["ok", "missing"])
    assert snapshots[0]["absolute_path"] == str(evidence_file)
    if depth >= 3:
        missing_snapshot = snapshots[1]
        assert missing_snapshot["absolute_path"] == str(missing_file)
        assert missing_snapshot["exists"] is False


@pytest.mark.asyncio
async def test_summary_consumes_only_the_remaining_scoring_budget(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    audit_root = tmp_path / "audit"
    _patch_audit_root(monkeypatch, audit_root)

    evidence_file = tmp_path / "summary-evidence.txt"
    evidence_file.write_text("small summary evidence", encoding="utf-8")
    missing_file = tmp_path / "summary-missing.txt"
    audited_call = InternalToolCall(
        id="summary-budget-shell-call",
        name="execute_shell",
        arguments={"command": f'python "{evidence_file}"', "execution_mode": "non_interactive"},
    )
    cfg = _audit_config(tmp_path, threshold=5)
    cfg.security.audit_depth = 3
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        if len(generate_calls) == 1:
            payload = _model_payload(kwargs)
            assert payload["remaining_file_reads"] == 3
            return _tool_response([_read_call("score-read", audited_call.id, evidence_file)])
        if len(generate_calls) == 2:
            results = _tool_results(kwargs["messages"])
            successful_read = next(item for item in results if item["status"] == "ok")
            return _score_response(audited_call.id, [_file_check(successful_read)])
        if len(generate_calls) == 3:
            payload = _model_payload(kwargs)
            assert payload["remaining_file_reads"] == 2
            assert payload["audit_results"][0]["score"] == 5
            assert payload["audit_results"][0]["deny"] is False
            return _tool_response(
                [
                    _read_call("summary-repeat", audited_call.id, evidence_file),
                    _read_call("summary-missing", audited_call.id, missing_file),
                ]
            )
        if len(generate_calls) == 4:
            assert kwargs["tools"] is None
            assert [item["remaining_file_reads"] for item in _tool_results(kwargs["messages"])] == [1, 0]
            return _text_response("确认后将执行该命令。")
        raise AssertionError("audit summary should use only the remaining reads")

    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)
    async with audit_database() as db:
        result = await _audit_round(db, cfg=cfg, tool_calls=[audited_call], working_directory=tmp_path)

    assert result is not None
    assert result.status is AuditRecordStatus.PENDING
    assert result.may_execute is False
    assert len(generate_calls) == 4
    assert len(_tool_results(generate_calls[3]["messages"])) == 2
    assert [item["status"] for item in _tool_results(generate_calls[3]["messages"])] == ["ok", "missing"]

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        assert record is not None
        assert record.status is AuditRecordStatus.PENDING
        assert record.context_file_path is not None
        context_path = Path(record.context_file_path)
        assert context_path.is_file()
        context = json.loads(context_path.read_text(encoding="utf-8"))

    assert context["audit_depth"] == 3
    assert context["file_read_calls"] == 3
    assert len(context["scoring_response"]["file_reads"]) == 1
    assert len(context["summary"]["file_reads"]) == 2
    assert [item["status"] for item in context["summary"]["file_reads"]] == ["ok", "missing"]
    assert all(item["tool_call_id"] == audited_call.id for item in context["summary"]["file_reads"])
    assert len(details) == 1
    assert details[0].conclusion is AuditToolConclusion.PENDING
    assert details[0].file_snapshots[0]["absolute_path"] == str(evidence_file)


@pytest.mark.parametrize("violating_phase", ["scoring", "summary"], ids=["scoring-fails", "summary-fallback"])
@pytest.mark.asyncio
async def test_exhausted_budget_rejects_additional_tool_calls_without_looping(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    violating_phase: str,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    audit_root = tmp_path / "audit"
    _patch_audit_root(monkeypatch, audit_root)

    evidence_file = tmp_path / "exhaustion-evidence.txt"
    evidence_file.write_text("small exhaustion evidence", encoding="utf-8")
    audited_call = InternalToolCall(
        id=f"{violating_phase}-exhaustion-call",
        name="execute_shell",
        arguments={"command": f'python "{evidence_file}"', "execution_mode": "non_interactive"},
    )
    cfg = _audit_config(tmp_path, threshold=5)
    cfg.security.audit_depth = 1
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        index = len(generate_calls)
        if violating_phase == "scoring":
            if index == 1:
                return _tool_response([_read_call("scoring-read", audited_call.id, evidence_file)])
            assert index == 2
            assert kwargs["tools"] is None
            return _tool_response([_read_call("scoring-overflow", audited_call.id, evidence_file)])
        if index == 1:
            return _audit_response([audited_call.id], deny=False, score=5)
        if index == 2:
            payload = _model_payload(kwargs)
            assert payload["remaining_file_reads"] == 1
            assert payload["audit_results"][0]["score"] == 5
            return _tool_response([_read_call("summary-read", audited_call.id, evidence_file)])
        assert index == 3
        assert kwargs["tools"] is None
        return _tool_response([_read_call("summary-overflow", audited_call.id, evidence_file)])

    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)
    async with audit_database() as db:
        result = await _audit_round(db, cfg=cfg, tool_calls=[audited_call], working_directory=tmp_path)

    assert result is not None
    assert len(generate_calls) == (2 if violating_phase == "scoring" else 3)
    assert generate_calls[-1]["tools"] is None
    if violating_phase == "scoring":
        assert result.status is AuditRecordStatus.AUDIT_FAILED
        assert result.may_execute is False
        assert json.loads(result.tool_results[0].content or "{}")["status"] == AuditRecordStatus.AUDIT_FAILED.value
    else:
        assert result.status is AuditRecordStatus.PENDING
        assert result.may_execute is False
        assert result.confirmation_payload is not None
        assert result.confirmation_payload["summary"]

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        assert record is not None
        assert record.status is result.status
        assert record.context_file_path is not None
        context_path = Path(record.context_file_path)
        assert context_path.is_file()
        context = json.loads(context_path.read_text(encoding="utf-8"))

    assert context["file_read_calls"] == 1
    assert len(details) == 1
    if violating_phase == "scoring":
        assert record.failure_type is AuditFailureType.AUDIT_SERVICE_FAILED
        assert details[0].conclusion is AuditToolConclusion.AUDIT_FAILED
        assert "summary" not in context
    else:
        assert details[0].conclusion is AuditToolConclusion.PENDING
        assert context["summary"]["fallback"] is True


@pytest.mark.asyncio
async def test_format_correction_preserves_evidence_budget_and_result_id_mapping(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")

    file_one = tmp_path / "evidence-one.txt"
    file_one.write_text("verified evidence for the first command", encoding="utf-8")
    file_two = tmp_path / "evidence-two.txt"
    file_two.write_text("verified evidence for the second command", encoding="utf-8")
    working_directory = tmp_path / "shell-working"
    working_directory.mkdir()
    environment_path = tmp_path / "shell-environment"
    monkeypatch.setenv("MONOLIGHT_AUDIT_TEST_PATH", str(environment_path))

    call_one = InternalToolCall(
        id="format-correction-shell-one",
        name=SHELL_TOOL_NAME,
        arguments={"command": "echo BUSINESS_COMMAND_MUST_NOT_RUN_ONE", "execution_mode": "non_interactive"},
    )
    call_two = InternalToolCall(
        id="format-correction-shell-two",
        name=SHELL_TOOL_NAME,
        arguments={"command": "echo BUSINESS_COMMAND_MUST_NOT_RUN_TWO", "execution_mode": "non_interactive"},
    )
    background_call = InternalToolCall(
        id="format-correction-background-call",
        name="list_background_tasks",
        arguments={},
    )
    tool_calls = [call_one, call_two, background_call]
    query_command = _environment_query_command()
    cfg = _audit_config(tmp_path, threshold=5)
    cfg.security.audit_depth = 3
    generate_calls: list[dict[str, Any]] = []
    shell_executions: list[tuple[str, str]] = []
    _record_real_shell_execution(monkeypatch, shell_executions)
    expected_summary = "操作: 确认后运行两条Shell命令，输出各自指定的业务标记。\n\n范围: 本轮提交的两项待审计命令。\n\n影响: 两条命令只输出文本，不修改文件。\n\n未核实: 暂无其他关键不确定项。"

    malformed_content = json.dumps(
        {
            "results": [
                {
                    "tool_call_id": call_one.id,
                    "score": 5,
                    "deny": False,
                    "reason": "first command assessed",
                    "file_checks": {},
                },
                {
                    "tool_call_id": call_two.id,
                    "score": 0,
                    "deny": False,
                    "reason": "second command assessed",
                    "file_checks": {},
                },
            ]
        },
        ensure_ascii=False,
    )

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        call_index = len(generate_calls)
        if call_index == 1:
            payload = _model_payload(kwargs)
            assert payload["audit_depth"] == 3
            assert payload["remaining_evidence_calls"] == 3
            assert [item["tool_call_id"] for item in payload["tool_calls"]] == [call_one.id, call_two.id]
            assert kwargs["tools"] is not None
            return _tool_response(
                [
                    _read_call("format-correction-read-one", call_one.id, file_one),
                    _shell_call("format-correction-query", call_two.id, query_command),
                ]
            )
        if call_index == 2:
            assert kwargs["tools"] is not None
            evidence = _tool_results(kwargs["messages"])
            assert [item["status"] for item in evidence] == ["ok", "ok"]
            assert evidence[0]["tool_call_id"] == call_one.id
            assert evidence[0]["content"] == "verified evidence for the first command"
            assert evidence[1]["tool_call_id"] == call_two.id
            return _text_response(malformed_content)
        if call_index == 3:
            assert kwargs["tools"] is not None
            messages = kwargs["messages"]
            assert messages[-2].role == MessageRole.ASSISTANT
            assert messages[-2].content == malformed_content
            assert messages[-1].role == MessageRole.USER
            correction = json.loads(messages[-1].content or "{}")
            assert correction["expected_tool_call_ids"] == [call_one.id, call_two.id]
            assert correction["remaining_evidence_calls"] == 1
            assert correction["remaining_file_reads"] == 1
            assert correction["remaining_shell_calls"] == 1

            evidence = _tool_results(messages)
            assert [item["tool_call_id"] for item in evidence] == [call_one.id, call_two.id]
            assert evidence[0]["content"] == "verified evidence for the first command"
            shell_payload = json.loads(evidence[1]["content"])
            assert str(environment_path) in shell_payload["stdout"]
            assert str(working_directory) in shell_payload["stdout"]
            return _tool_response([_read_call("format-correction-read-two", call_two.id, file_two)])
        if call_index == 4:
            assert kwargs["tools"] is None
            evidence = _tool_results(kwargs["messages"])
            assert [item["tool_call_id"] for item in evidence] == [call_one.id, call_two.id, call_two.id]
            assert evidence[0]["content"] == "verified evidence for the first command"
            assert evidence[2]["content"] == "verified evidence for the second command"
            assert any(message.content == malformed_content for message in kwargs["messages"])

            read_one = next(item for item in evidence if item.get("absolute_path") == str(file_one))
            read_two = next(item for item in evidence if item.get("absolute_path") == str(file_two))
            return _text_response(
                json.dumps(
                    {
                        "results": [
                            {
                                "tool_call_id": call_two.id,
                                "score": 0,
                                "deny": False,
                                "reason": "second command has no identified risk",
                                "file_checks": [_file_check(read_two)],
                            },
                            {
                                "tool_call_id": call_one.id,
                                "score": 5,
                                "deny": False,
                                "reason": "first command requires confirmation",
                                "file_checks": [_file_check(read_one)],
                            },
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        assert call_index == 5
        assert kwargs["tools"] is None
        summary_payload = _model_payload(kwargs)
        assert [item["tool_call_id"] for item in summary_payload["audit_results"]] == [
            call_one.id,
            call_two.id,
            background_call.id,
        ]
        assert [item["score"] for item in summary_payload["audit_results"]] == [5, 0, 0]
        assert [item["content"] for item in summary_payload["file_reads"]] == [
            "verified evidence for the first command",
            "verified evidence for the second command",
        ]
        assert len(summary_payload["shell_results"]) == 1
        summary_shell_payload = json.loads(summary_payload["shell_results"][0]["content"])
        assert str(environment_path) in summary_shell_payload["stdout"]
        assert str(working_directory) in summary_shell_payload["stdout"]
        return _text_response(expected_summary)

    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)
    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=cfg,
            tool_calls=tool_calls,
            working_directory=working_directory,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.PENDING
    assert result.may_execute is False
    assert len(generate_calls) == 5
    assert shell_executions == [(query_command, "non_interactive")]
    assert [message.tool_call_id for message in result.tool_results] == [call_one.id, call_two.id, background_call.id]
    expected_tool_result = {
        "status": AuditRecordStatus.PENDING.value,
        "error": t(MSG_AUDIT_WAITING_CONFIRMATION),
    }
    assert all(json.loads(message.content or "{}") == expected_tool_result for message in result.tool_results)
    assert result.confirmation_payload is not None
    assert result.confirmation_payload["summary"] == expected_summary
    assert expected_summary in result.confirmation_payload["plain_text"]

    async with audit_database() as db:
        records = (await db.scalars(select(AuditRecord))).all()
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        assert record is not None
        assert record.context_file_path is not None
        context = json.loads(Path(record.context_file_path).read_text(encoding="utf-8"))

    assert len(records) == 1
    assert records[0].id == result.audit_record_id
    assert record.intent_summary == expected_summary
    assert context["audit_depth"] == 3
    assert context["file_read_calls"] == 2
    assert context["shell_calls"] == 1
    assert context["evidence_calls"] == 3
    scoring_response = context["scoring_response"]
    parsed_results_by_id = {item["tool_call_id"]: item for item in scoring_response["parsed"]["results"]}
    reads_by_id = {item["tool_call_id"]: item for item in scoring_response["file_reads"]}
    assert [item["tool_call_id"] for item in scoring_response["parsed"]["results"]] == [call_two.id, call_one.id]
    assert [item["score"] for item in scoring_response["parsed"]["results"]] == [0, 5]
    assert parsed_results_by_id[call_one.id]["file_checks"] == [_file_check(reads_by_id[call_one.id])]
    assert parsed_results_by_id[call_two.id]["file_checks"] == [_file_check(reads_by_id[call_two.id])]
    assert [item["content"] for item in scoring_response["file_reads"]] == [
        "verified evidence for the first command",
        "verified evidence for the second command",
    ]
    assert len(scoring_response["shell_results"]) == 1
    saved_shell_payload = json.loads(scoring_response["shell_results"][0]["content"])
    assert str(environment_path) in saved_shell_payload["stdout"]
    assert str(working_directory) in saved_shell_payload["stdout"]
    assert context["summary"]["response"] == expected_summary
    assert context["summary"]["file_reads"] == []
    assert context["summary"]["shell_results"] == []
    assert [detail.original_tool_call_id for detail in details] == [call_one.id, call_two.id, background_call.id]

    details_by_id = {detail.original_tool_call_id: detail for detail in details}
    assert details_by_id[call_one.id].score == 5
    assert details_by_id[call_one.id].file_snapshots[0]["absolute_path"] == str(file_one)
    assert details_by_id[call_one.id].file_snapshots[0]["content"] == "verified evidence for the first command"
    assert details_by_id[call_two.id].score == 0
    assert details_by_id[call_two.id].file_snapshots[0]["absolute_path"] == str(file_two)
    assert details_by_id[call_two.id].file_snapshots[0]["content"] == "verified evidence for the second command"


@pytest.mark.parametrize(
    "invalid_evidence_case",
    ["interactive", "extra_argument", "foreign_tool_call_id", "missing_command", "empty_command"],
)
@pytest.mark.asyncio
async def test_invalid_shell_evidence_consumes_budget_without_execution_or_recursion(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_evidence_case: str,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")

    working_directory = tmp_path / "shell-working"
    working_directory.mkdir()
    audited_call = InternalToolCall(
        id="invalid-evidence-business-shell-call",
        name=SHELL_TOOL_NAME,
        arguments={
            "command": "echo BUSINESS_COMMAND_MUST_NOT_RUN",
            "execution_mode": "non_interactive",
        },
    )
    evidence_arguments: dict[str, Any] = {
        "command": "echo INVALID_EVIDENCE_COMMAND_MUST_NOT_RUN",
        "execution_mode": "non_interactive",
        "tool_call_id": audited_call.id,
    }
    if invalid_evidence_case == "interactive":
        evidence_arguments["execution_mode"] = "interactive"
    elif invalid_evidence_case == "extra_argument":
        evidence_arguments["run_in_background"] = True
    elif invalid_evidence_case == "foreign_tool_call_id":
        evidence_arguments["tool_call_id"] = "another-audit-round-call"
    elif invalid_evidence_case == "missing_command":
        del evidence_arguments["command"]
    else:
        assert invalid_evidence_case == "empty_command"
        evidence_arguments["command"] = ""
    invalid_evidence_call = InternalToolCall(
        id=f"invalid-evidence-{invalid_evidence_case}",
        name=SHELL_TOOL_NAME,
        arguments=evidence_arguments,
    )

    cfg = _audit_config(tmp_path, threshold=5)
    cfg.security.audit_depth = 1
    generate_calls: list[dict[str, Any]] = []
    shell_execute_calls: list[tuple[str, str]] = []
    invalid_results: list[dict[str, Any]] = []
    expected_summary = "操作: 确认后执行该Shell命令并输出指定标记文本。\n\n范围: 本轮提交的一项Shell命令。\n\n影响: 只输出文本，不修改文件。\n\n未核实: 取证参数不合法，尚未核实该环境信息。"

    async def record_shell_execution(
        _executor: ShellExecutor,
        command: str,
        execution_mode: str,
    ) -> str:
        shell_execute_calls.append((command, execution_mode))
        return json.dumps({"exit_code": 0, "stdout": "SHOULD_NOT_EXECUTE", "stderr": ""})

    monkeypatch.setattr(ShellExecutor, "execute", record_shell_execution)

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        call_index = len(generate_calls)
        if call_index == 1:
            payload = _model_payload(kwargs)
            assert payload["audit_depth"] == 1
            assert payload["remaining_evidence_calls"] == 1
            assert [item["tool_call_id"] for item in payload["tool_calls"]] == [audited_call.id]
            assert kwargs["tools"] is not None
            return _tool_response([invalid_evidence_call])
        if call_index == 2:
            assert kwargs["tools"] is None
            evidence = _tool_results(kwargs["messages"])
            assert len(evidence) == 1
            assert evidence[0]["status"] == "invalid"
            evidence_error = json.loads(evidence[0]["content"])["error"]
            assert isinstance(evidence_error, str) and evidence_error
            assert evidence[0]["tool_call_id"] == evidence_arguments.get("tool_call_id")
            assert evidence[0]["remaining_evidence_calls"] == 0
            assert evidence[0]["remaining_shell_calls"] == 0
            assert evidence[0]["remaining_file_reads"] == 0
            invalid_results.append(evidence[0])
            return _audit_response([audited_call.id], deny=False, score=5)
        assert call_index == 3
        assert kwargs["tools"] is None
        summary_payload = _model_payload(kwargs)
        assert summary_payload["shell_results"] == invalid_results
        assert summary_payload["audit_results"][0]["tool_call_id"] == audited_call.id
        return _text_response(expected_summary)

    monkeypatch.setattr("app.core.audit.service.LLMClient.generate", generate)
    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=cfg,
            tool_calls=[audited_call],
            working_directory=working_directory,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.PENDING
    assert result.may_execute is False
    assert len(generate_calls) == 3
    assert shell_execute_calls == []
    assert len(result.tool_results) == 1
    assert json.loads(result.tool_results[0].content or "{}") == {
        "status": AuditRecordStatus.PENDING.value,
        "error": t(MSG_AUDIT_WAITING_CONFIRMATION),
    }
    assert result.confirmation_payload is not None
    assert result.confirmation_payload["summary"] == expected_summary
    assert expected_summary in result.confirmation_payload["plain_text"]

    async with audit_database() as db:
        records = (await db.scalars(select(AuditRecord))).all()
        record = await audit_crud.get_record(db, result.audit_record_id)
        executions = (await db.scalars(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == result.audit_record_id))).all()
        assert record is not None
        assert record.context_file_path is not None
        context = json.loads(Path(record.context_file_path).read_text(encoding="utf-8"))

    assert len(records) == 1
    assert records[0].id == result.audit_record_id
    assert record.status is AuditRecordStatus.PENDING
    assert executions == []
    assert context["file_read_calls"] == 0
    assert context["shell_calls"] == 1
    assert context["evidence_calls"] == 1
    invalid_evidence = context["scoring_response"]["shell_results"]
    assert len(invalid_evidence) == 1
    assert invalid_evidence[0]["status"] == "invalid"
    evidence_error = json.loads(invalid_evidence[0]["content"])["error"]
    assert evidence_error
    assert invalid_evidence[0]["tool_call_id"] == evidence_arguments.get("tool_call_id")
    assert "SHOULD_NOT_EXECUTE" not in json.dumps(invalid_evidence)
    assert context["summary"]["response"] == expected_summary
