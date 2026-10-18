import json
from pathlib import Path
from typing import Any

import pytest

from app.core.constants import READ_TEXT_FILE_TOOL_NAME
from app.core.crud.audit.audit import audit_crud
from app.models.audit import AuditFailureType, AuditRecordStatus, AuditToolConclusion
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
