import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

import app.core.audit.service as audit_service_module
from app.core import paths
from app.core.audit.persistence import persist_prepared_audit_round
from app.core.constants import ERR_AUDIT_SERVICE_FAILED_STOP
from app.core.crud.audit.audit import audit_crud
from app.core.i18n import t
from app.core.prompts import (
    AUDIT_BATCH_PROMPT,
    AUDIT_FORMAT_CORRECTION_PROMPT,
    AUDIT_STANDARD_BATCH_PROMPT,
    AUDIT_SUMMARY_PROMPT,
)
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.core.tools.shell import ShellExecutor
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditFailureType,
    AuditRecordStatus,
    AuditToolConclusion,
)
from app.models.channel import ModelChannel
from app.models.message import (
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    Message,
    MessageRole,
    MessageType,
)
from app.models.profile import ProfileConfig
from app.models.terminal_session import (
    TerminalControlCommand,
    TerminalControlCommandStatus,
    TerminalSession,
)

AUDIT_MODEL_ID = "directory-policy-audit-model"
UID = "directory-policy-user"
SESSION_ID = "directory-policy-session"
TERMINAL_SESSION_ID = "terminal-audit-policy-session-0001"
_MISSING = object()


@pytest.fixture(autouse=True)
def isolate_system_protection_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "SKILLS_DIR", tmp_path / "data" / "skills")


@pytest.fixture
async def audit_database(database_factory):
    async with database_factory(name="audit-directory-policy.sqlite3") as session_factory:
        yield session_factory


def _audit_config(
    tmp_path: Path,
    *,
    threshold: int = 0,
    allowed_operation_dirs: list[str] | None = None,
    protect_system_directories: bool | None = None,
) -> ProfileConfig:
    security = {
        "audit_channel_id": 1,
        "audit_model_id": AUDIT_MODEL_ID,
        "audit_threshold": threshold,
        "audit_report_language": "zh",
    }
    if protect_system_directories is not None:
        security["protect_system_directories"] = protect_system_directories

    return ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "rules": [
                        {
                            "channel_id": 1,
                            "model_id": AUDIT_MODEL_ID,
                            "priority": 1,
                            "weight": 1,
                        }
                    ]
                }
            },
            "security": security,
            "tool": {
                "enabled_tools": ["execute_shell", "file_tool"],
                "allowed_operation_dirs": (allowed_operation_dirs if allowed_operation_dirs is not None else [str(tmp_path)]),
            },
            "other": {},
            "memory": {"enabled": False},
        }
    )


def _audit_channel() -> ModelChannel:
    return ModelChannel(
        id=1,
        name="directory-policy-audit-channel",
        api_key="enc:v1:test-key",
        base_url="https://llm.invalid",
        model_ids=[
            {
                "model_id": AUDIT_MODEL_ID,
                "usage": "CHAT",
                "protocol": "OPENAI",
                "context_window_k": 32,
                "max_tokens": 512,
                "temperature": 0,
                "top_p": 1,
            }
        ],
    )


async def _seed_audit_channel(session_factory) -> None:
    async with session_factory() as db:
        db.add(_audit_channel())
        await db.commit()


def _patch_audit_root(monkeypatch: pytest.MonkeyPatch, audit_root: Path) -> None:
    async def persist_in_test_root(db, **kwargs: Any) -> bool:
        return await persist_prepared_audit_round(db, audit_root=audit_root, **kwargs)

    monkeypatch.setattr(audit_service_module, "persist_prepared_audit_round", persist_in_test_root)


def _audit_response(
    tool_call_ids: list[str],
    *,
    deny: Any = True,
    score: int = 0,
    include_deny: bool = True,
) -> InternalResponse:
    results = []
    for tool_call_id in tool_call_ids:
        result = {
            "tool_call_id": tool_call_id,
            "score": score,
            "reason": "directory policy test result",
            "file_checks": [],
        }
        if include_deny:
            result["deny"] = deny
        results.append(result)
    return InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content=json.dumps({"results": results}, ensure_ascii=False),
        ),
        model=AUDIT_MODEL_ID,
        usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        finish_reason="stop",
    )


async def _audit_round(db, *, cfg: ProfileConfig, tool_calls: list[InternalToolCall], working_directory: Path):
    return await audit_service_module.audit_tool_round(
        db,
        cfg=cfg,
        tool_calls=tool_calls,
        source_assistant_message_id=101,
        uid=UID,
        operator_username="tester",
        session_id=SESSION_ID,
        source="integration",
        language="zh",
        working_directory=working_directory,
        execution_scope="foreground",
    )


async def _seed_terminal_history(session_factory, working_directory: Path) -> None:
    async with session_factory() as db:
        db.add(
            TerminalSession(
                terminal_session_id=TERMINAL_SESSION_ID,
                uid=UID,
                session_id=SESSION_ID,
                original_tool_call_id="initial-shell-call",
                profile_id=1,
                command="cmd.exe /Q",
                working_directory=str(working_directory),
                status=TerminalSessionStatus.RUNNING,
                allowed_actions=[action.value for action in TerminalAction],
            )
        )
        db.add_all(
            [
                TerminalControlCommand(
                    terminal_session_id=TERMINAL_SESSION_ID,
                    request_id="terminal-history-write",
                    action=TerminalAction.WRITE,
                    payload={"data": "echo previous"},
                    payload_hash="a" * 64,
                    status=TerminalControlCommandStatus.SUCCEEDED,
                    result={"bytes_written": 13},
                ),
                TerminalControlCommand(
                    terminal_session_id=TERMINAL_SESSION_ID,
                    request_id="terminal-history-read",
                    action=TerminalAction.READ,
                    payload={"offset": 0, "max_bytes": 65536},
                    payload_hash="b" * 64,
                    status=TerminalControlCommandStatus.SUCCEEDED,
                    result={"read_status": "ok", "output": "previous output", "output_truncated": False},
                ),
            ]
        )
        await db.commit()


@pytest.mark.parametrize(
    ("tool_name", "execution_mode"),
    [
        ("execute_shell", "non_interactive"),
        ("execute_shell", "interactive"),
        ("terminal_tool", None),
    ],
    ids=["shell-non-interactive", "shell-interactive", "terminal-write"],
)
@pytest.mark.parametrize("threshold", [0, 5], ids=["threshold-0", "threshold-5"])
@pytest.mark.parametrize(
    ("violation", "protect_system_directories"),
    [
        ("protected_write", True),
        ("outside_write", True),
        ("outside_write", False),
    ],
    ids=["protected-write", "outside-write-protected", "outside-write-unprotected"],
)
@pytest.mark.asyncio
async def test_model_denial_blocks_entire_tool_round_and_cannot_be_claimed(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_name: str,
    execution_mode: str | None,
    threshold: int,
    violation: str,
    protect_system_directories: bool,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    audit_root = tmp_path / "audit"
    _patch_audit_root(monkeypatch, audit_root)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if violation == "protected_write":
        target_file = tmp_path / "data" / "skills" / "protected.txt"
        allowed_operation_dirs = [str(tmp_path)]
    else:
        target_file = tmp_path / "outside" / "target.txt"
        allowed_operation_dirs = [str(workspace)]
    target_file.parent.mkdir(parents=True)
    target_file.write_text("sentinel", encoding="utf-8")
    command = f'echo changed > "{target_file}"'
    if tool_name == "terminal_tool":
        await _seed_terminal_history(audit_database, workspace)
        arguments = {
            "operation": "write",
            "terminal_session_id": TERMINAL_SESSION_ID,
            "data": f"{command}\n",
        }
    else:
        arguments = {
            "command": command,
            "execution_mode": execution_mode,
        }

    audited_call = InternalToolCall(id="audited-call", name=tool_name, arguments=arguments)
    background_call = InternalToolCall(
        id="background-list-call",
        name="manage_background_tasks",
        arguments={"operation": "list"},
    )
    tool_calls = [audited_call, background_call]
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        return _audit_response([audited_call.id], deny=True, score=0)

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)
    cfg = _audit_config(
        tmp_path,
        threshold=threshold,
        allowed_operation_dirs=allowed_operation_dirs,
        protect_system_directories=protect_system_directories,
    )
    assert cfg.security.protect_system_directories is protect_system_directories

    async with audit_database() as db:
        result = await _audit_round(db, cfg=cfg, tool_calls=tool_calls, working_directory=workspace)

    assert result is not None
    assert result.status is AuditRecordStatus.BLOCKED
    assert result.may_execute is False
    assert result.confirmation_payload is None
    assert len(generate_calls) == 1
    assert [message.tool_call_id for message in result.tool_results] == ["audited-call", "background-list-call"]
    expected_prompt = AUDIT_BATCH_PROMPT if protect_system_directories else AUDIT_STANDARD_BATCH_PROMPT
    assert generate_calls[0]["messages"][0].content == expected_prompt
    request_payload = json.loads(generate_calls[0]["messages"][1].content)
    directory_policy = request_payload["directory_policy"]
    expected_allowed_operation_dirs = list(dict.fromkeys([*allowed_operation_dirs, str(workspace)]))
    assert directory_policy["allowed_operation_dirs"] == expected_allowed_operation_dirs
    if protect_system_directories:
        assert str(tmp_path / "data" / "skills") in directory_policy["write_protected_directories"]
    else:
        assert "write_protected_directories" not in directory_policy
    assert all(json.loads(message.content or "{}")["status"] == AuditRecordStatus.BLOCKED.value for message in result.tool_results)
    assert target_file.read_text(encoding="utf-8") == "sentinel"

    if tool_name == "terminal_tool":
        request_payload = json.loads(generate_calls[0]["messages"][1].content)
        terminal_context = request_payload["tool_calls"][0]["terminal_context"]
        assert terminal_context["status"] == TerminalSessionStatus.RUNNING.value
        assert [item["action"] for item in terminal_context["history"]] == ["write", "read"]
        assert terminal_context["history"][0]["data"] == "echo previous"
        assert terminal_context["history"][1]["output"] == "previous output"

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == result.audit_record_id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == result.audit_record_id))).scalars().all())
        cards = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.uid == UID,
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.AUDIT_CONFIRMATION,
                    )
                )
            )
            .scalars()
            .all()
        )

        assert record is not None
        assert record.status is AuditRecordStatus.BLOCKED
        assert record.execution_claim_token is None
        assert record.context_file_path is not None
        context_path = Path(record.context_file_path)
        assert context_path.is_file()
        assert context_path.parent == audit_root / f"temp_{UID}"
        assert len(details) == 2
        audited_detail = next(detail for detail in details if detail.original_tool_call_id == "audited-call")
        assert audited_detail.conclusion is AuditToolConclusion.BLOCKED
        assert audited_detail.score == 0
        assert claims == []
        assert executions == []
        assert cards == []
        passed_claim, passed_token = await audit_crud.claim_passed_for_execution(
            db,
            audit_record_id=result.audit_record_id,
        )
        assert passed_claim is None and passed_token is None
        for message_id, decision in ((201, "ignore"), (202, "approve")):
            pending_claim, pending_token = await audit_crud.claim_pending_for_execution(
                db,
                audit_record_id=result.audit_record_id,
                uid=UID,
                session_id=SESSION_ID,
                decision_message_id=message_id,
                decision_raw_message=decision,
                decided_by="tester",
            )
            assert pending_claim is None and pending_token is None

        persisted_record = await audit_crud.get_record(db, result.audit_record_id)
        claims_after_attempts = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == result.audit_record_id))).scalars().all())
        executions_after_attempts = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == result.audit_record_id))).scalars().all())
        assert persisted_record is not None
        assert persisted_record.status is AuditRecordStatus.BLOCKED
        assert claims_after_attempts == []
        assert executions_after_attempts == []
        if tool_name == "terminal_tool":
            terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
            terminal_commands = list((await db.execute(select(TerminalControlCommand).where(TerminalControlCommand.terminal_session_id == TERMINAL_SESSION_ID))).scalars().all())
            assert terminal_session is not None
            assert terminal_session.status is TerminalSessionStatus.RUNNING
            assert len(terminal_commands) == 2


@pytest.mark.parametrize(
    "error_kind",
    [
        "non_json",
        "duplicate_id",
        "missing_item",
        "unknown_id",
        "file_checks_dict",
        "boolean_score",
        "provider_exception",
    ],
    ids=[
        "non-json",
        "duplicate-id",
        "missing-item",
        "unknown-id",
        "file-checks-dict",
        "boolean-score",
        "provider-exception",
    ],
)
@pytest.mark.asyncio
async def test_invalid_audit_batch_retries_once_then_fails_closed_without_leaking_details(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error_kind: str,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    audit_root = tmp_path / "audit"
    _patch_audit_root(monkeypatch, audit_root)

    private_marker = "private-audit-diagnostic-marker"
    tool_calls = [
        InternalToolCall(
            id="batch-shell-call-1",
            name="execute_shell",
            arguments={"command": "echo first command must not execute", "execution_mode": "non_interactive"},
        ),
        InternalToolCall(
            id="batch-shell-call-2",
            name="execute_shell",
            arguments={"command": "echo second command must not execute", "execution_mode": "non_interactive"},
        ),
        InternalToolCall(id="batch-background-list-call", name="manage_background_tasks", arguments={"operation": "list"}),
    ]
    audited_call_ids = [tool_calls[0].id, tool_calls[1].id]
    shell_executions: list[tuple[str, str]] = []

    async def count_shell_execution(_executor: Any, command: str, execution_mode: str) -> str:
        shell_executions.append((command, execution_mode))
        return "unexpected shell execution"

    monkeypatch.setattr(ShellExecutor, "execute", count_shell_execution)
    model_requests: list[list[InternalMessage]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        model_requests.append([message.model_copy(deep=True) for message in kwargs["messages"]])
        if error_kind == "provider_exception" and len(model_requests) == 2:
            raise RuntimeError(private_marker)

        response = _audit_response(audited_call_ids, score=7)
        payload = json.loads(response.message.content)
        for item in payload["results"]:
            item["reason"] = private_marker
            item["file_checks"] = [{"detail": private_marker}]

        response_error_kind = "non_json" if error_kind == "provider_exception" else error_kind
        if response_error_kind == "non_json":
            content = f"not JSON: {private_marker}"
            return response.model_copy(update={"message": response.message.model_copy(update={"content": content})})
        if response_error_kind == "duplicate_id":
            payload["results"][1]["tool_call_id"] = payload["results"][0]["tool_call_id"]
        elif response_error_kind == "missing_item":
            payload["results"].pop()
        elif response_error_kind == "unknown_id":
            payload["results"][1]["tool_call_id"] = f"unknown-{private_marker}"
        elif response_error_kind == "file_checks_dict":
            payload["results"][0]["file_checks"] = {"detail": private_marker}
        elif response_error_kind == "boolean_score":
            payload["results"][0]["score"] = True

        content = json.dumps(payload, ensure_ascii=False)
        return response.model_copy(update={"message": response.message.model_copy(update={"content": content})})

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)

    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=_audit_config(tmp_path, threshold=0, protect_system_directories=True),
            tool_calls=tool_calls,
            working_directory=tmp_path,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.AUDIT_FAILED
    assert result.may_execute is False
    assert result.confirmation_payload is None
    assert len(model_requests) == 2
    assert [messages[0].content for messages in model_requests] == [AUDIT_BATCH_PROMPT, AUDIT_BATCH_PROMPT]
    correction_request = json.loads(model_requests[1][-1].content or "{}")
    assert correction_request["instruction"] == AUDIT_FORMAT_CORRECTION_PROMPT
    assert [message.tool_call_id for message in result.tool_results] == [call.id for call in tool_calls]
    expected_public_result = {
        "status": AuditRecordStatus.AUDIT_FAILED.value,
        "error": t(ERR_AUDIT_SERVICE_FAILED_STOP),
    }
    public_results = [json.loads(message.content or "{}") for message in result.tool_results]
    assert public_results == [expected_public_result] * len(tool_calls)
    assert all("score" not in item and "file_checks" not in item for item in public_results)
    assert private_marker not in " ".join(message.content or "" for message in result.tool_results)
    assert shell_executions == []

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == result.audit_record_id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == result.audit_record_id))).scalars().all())

        assert record is not None
        assert record.status is AuditRecordStatus.AUDIT_FAILED
        assert record.failure_type is AuditFailureType.AUDIT_SERVICE_FAILED
        assert record.error_reason
        assert record.error_reason != expected_public_result["error"]
        if error_kind == "provider_exception":
            assert private_marker in record.error_reason
        assert record.context_file_path is not None
        context_path = Path(record.context_file_path)
        assert context_path.is_file()
        assert context_path.parent == audit_root / f"temp_{UID}"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        assert context["scoring_response"]["error"] == record.error_reason
        assert len(details) == len(tool_calls)
        assert {detail.original_tool_call_id for detail in details} == {call.id for call in tool_calls}
        assert all(detail.conclusion is AuditToolConclusion.AUDIT_FAILED for detail in details)
        assert all(detail.score is None for detail in details)
        assert all(detail.reason == record.error_reason for detail in details)
        assert claims == []
        assert executions == []


@pytest.mark.parametrize(
    ("threshold", "score"),
    [(0, 9), (5, 5)],
    ids=["high-risk", "threshold-exceeded"],
)
@pytest.mark.parametrize("report_language", ["zh", "en"], ids=["chinese", "english"])
@pytest.mark.asyncio
async def test_pending_confirmation_preserves_structured_summary_and_directory_policy(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    threshold: int,
    score: int,
    report_language: str,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")
    expected_summary = {
        "zh": "操作: 确认后将更新一个共享文件。\n\n范围: 技能目录中的 protected.txt。\n\n影响: 文件原有内容会被替换。\n\n未核实: 尚未确认是否存在备份。",
        "en": "Action: After confirmation, one shared file will be updated.\n\nScope: protected.txt in the skills directory.\n\nImpact: The file's existing contents will be replaced.\n\nUnverified: Whether a backup exists has not been confirmed.",
    }[report_language]
    protected_file = tmp_path / "data" / "skills" / "protected.txt"
    protected_file.parent.mkdir(parents=True)
    protected_file.write_text("sentinel", encoding="utf-8")
    tool_call = InternalToolCall(
        id="high-risk-shell-call",
        name="execute_shell",
        arguments={"command": f'echo changed > "{protected_file}"', "execution_mode": "non_interactive"},
    )
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        if len(generate_calls) == 1:
            return _audit_response([tool_call.id], deny=False, score=score)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content=expected_summary),
            model=AUDIT_MODEL_ID,
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            finish_reason="stop",
        )

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)
    cfg = _audit_config(tmp_path, threshold=threshold, protect_system_directories=False)
    cfg.security.audit_report_language = report_language

    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=cfg,
            tool_calls=[tool_call],
            working_directory=tmp_path,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.PENDING
    assert result.may_execute is False
    assert result.confirmation_payload is not None
    assert len(generate_calls) == 2
    assert generate_calls[0]["messages"][0].content == AUDIT_STANDARD_BATCH_PROMPT
    score_payload = json.loads(generate_calls[0]["messages"][1].content)
    assert score_payload["directory_policy"] == {"allowed_operation_dirs": [str(tmp_path)]}
    assert generate_calls[1]["messages"][0].content == AUDIT_SUMMARY_PROMPT.format(audit_report_language=report_language)
    summary_payload = json.loads(generate_calls[1]["messages"][1].content)
    assert summary_payload["directory_policy"] == {"allowed_operation_dirs": [str(tmp_path)]}
    assert summary_payload["directory_policy"] == score_payload["directory_policy"]
    assert "write_protected_directories" not in summary_payload["directory_policy"]
    assert protected_file.read_text(encoding="utf-8") == "sentinel"
    assert result.confirmation_payload["summary"] == expected_summary
    assert expected_summary in result.confirmation_payload["plain_text"]
    assert result.confirmation_payload["confirmation_mode"] == ("high_risk_override" if score >= 8 else "standard")

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)

        assert record is not None
        assert record.status is AuditRecordStatus.PENDING
        assert record.intent_summary == expected_summary
        assert record.context_file_path is not None
        context_path = Path(record.context_file_path)
        assert context_path.is_file()
        context = json.loads(context_path.read_text(encoding="utf-8"))
        assert context["summary"]["response"] == expected_summary
        assert len(details) == 1
        assert details[0].conclusion is AuditToolConclusion.PENDING
        assert details[0].score == score


@pytest.mark.parametrize(
    ("include_deny", "deny"),
    [(False, _MISSING), (True, "false")],
    ids=["missing-deny", "non-bool-deny"],
)
@pytest.mark.parametrize(
    "protect_system_directories",
    [True, False],
    ids=["protection-enabled", "protection-disabled"],
)
@pytest.mark.asyncio
async def test_missing_or_non_boolean_deny_fails_closed_without_execution(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    include_deny: bool,
    deny: Any,
    protect_system_directories: bool,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    audit_root = tmp_path / "audit"
    _patch_audit_root(monkeypatch, audit_root)
    tool_call = InternalToolCall(
        id="malformed-deny-call",
        name="execute_shell",
        arguments={"command": "echo must-not-run", "execution_mode": "non_interactive"},
    )
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        return _audit_response(
            [tool_call.id],
            deny=None if deny is _MISSING else deny,
            include_deny=include_deny,
        )

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)

    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=_audit_config(tmp_path, threshold=0, protect_system_directories=protect_system_directories),
            tool_calls=[tool_call],
            working_directory=tmp_path,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.AUDIT_FAILED
    assert result.may_execute is False
    assert result.confirmation_payload is None
    assert len(generate_calls) == 2
    assert json.loads(result.tool_results[0].content or "{}")["status"] == AuditRecordStatus.AUDIT_FAILED.value

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == result.audit_record_id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == result.audit_record_id))).scalars().all())
        cards = list(
            (
                await db.execute(
                    select(Message).where(
                        Message.uid == UID,
                        Message.session_id == SESSION_ID,
                        Message.type == MessageType.AUDIT_CONFIRMATION,
                    )
                )
            )
            .scalars()
            .all()
        )

        assert record is not None
        assert record.status is AuditRecordStatus.AUDIT_FAILED
        assert record.failure_type is AuditFailureType.AUDIT_SERVICE_FAILED
        assert record.execution_claim_token is None
        assert len(details) == 1
        assert details[0].conclusion is AuditToolConclusion.AUDIT_FAILED
        assert details[0].score is None
        assert claims == []
        assert executions == []
        assert cards == []
