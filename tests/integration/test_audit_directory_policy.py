import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

import app.core.audit.service as audit_service_module
from app.core.audit.persistence import persist_prepared_audit_round
from app.core.crud.audit.audit import audit_crud
from app.core.prompts import AUDIT_BATCH_PROMPT, AUDIT_STANDARD_BATCH_PROMPT, AUDIT_SUMMARY_PROMPT
from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
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
                "enabled_tools": ["execute_shell", "file_tool", "terminal_write"],
                "allowed_operation_dirs": allowed_operation_dirs or [str(tmp_path)],
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
        ("terminal_write", None),
    ],
    ids=["shell-non-interactive", "shell-interactive", "terminal-write"],
)
@pytest.mark.parametrize("threshold", [0, 5], ids=["threshold-0", "threshold-5"])
@pytest.mark.asyncio
async def test_model_denial_blocks_entire_tool_round_and_cannot_be_claimed(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_name: str,
    execution_mode: str | None,
    threshold: int,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    audit_root = tmp_path / "audit"
    _patch_audit_root(monkeypatch, audit_root)

    protected_file = tmp_path / "data" / "skills" / "protected.txt"
    protected_file.parent.mkdir(parents=True)
    protected_file.write_text("sentinel", encoding="utf-8")
    if tool_name == "terminal_write":
        await _seed_terminal_history(audit_database, tmp_path)
        arguments = {
            "terminal_session_id": TERMINAL_SESSION_ID,
            "data": f'echo changed > "{protected_file}"\n',
        }
    else:
        arguments = {
            "command": f'echo changed > "{protected_file}"',
            "execution_mode": execution_mode,
        }

    audited_call = InternalToolCall(id="audited-call", name=tool_name, arguments=arguments)
    background_call = InternalToolCall(
        id="background-list-call",
        name="list_background_tasks",
        arguments={},
    )
    tool_calls = [audited_call, background_call]
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        return _audit_response([audited_call.id], deny=True, score=0)

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)
    cfg = _audit_config(tmp_path, threshold=threshold)
    assert cfg.security.protect_system_directories is True

    async with audit_database() as db:
        result = await _audit_round(db, cfg=cfg, tool_calls=tool_calls, working_directory=tmp_path)

    assert result is not None
    assert result.status is AuditRecordStatus.BLOCKED
    assert result.may_execute is False
    assert result.confirmation_payload is None
    assert len(generate_calls) == 1
    assert [message.tool_call_id for message in result.tool_results] == ["audited-call", "background-list-call"]
    assert generate_calls[0]["messages"][0].content == AUDIT_BATCH_PROMPT
    request_payload = json.loads(generate_calls[0]["messages"][1].content)
    directory_policy = request_payload["directory_policy"]
    assert directory_policy["allowed_operation_dirs"] == [str(tmp_path)]
    assert str(tmp_path / "data" / "skills") in directory_policy["write_protected_directories"]
    assert all(json.loads(message.content or "{}")["status"] == AuditRecordStatus.BLOCKED.value for message in result.tool_results)
    assert protected_file.read_text(encoding="utf-8") == "sentinel"

    if tool_name == "terminal_write":
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
        if tool_name == "terminal_write":
            terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
            terminal_commands = list((await db.execute(select(TerminalControlCommand).where(TerminalControlCommand.terminal_session_id == TERMINAL_SESSION_ID))).scalars().all())
            assert terminal_session is not None
            assert terminal_session.status is TerminalSessionStatus.RUNNING
            assert len(terminal_commands) == 2


@pytest.mark.parametrize(
    ("include_deny", "deny"),
    [(False, _MISSING), (True, True)],
    ids=["missing-deny", "extra-deny-true"],
)
@pytest.mark.asyncio
async def test_disabled_system_protection_allows_shell_audit_without_deny_field(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    include_deny: bool,
    deny: Any,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")
    protected_file = tmp_path / "data" / "skills" / "protected.txt"
    protected_file.parent.mkdir(parents=True)
    protected_file.write_text("sentinel", encoding="utf-8")
    tool_call = InternalToolCall(
        id="unprotected-shell-call",
        name="execute_shell",
        arguments={"command": f'echo changed > "{protected_file}"', "execution_mode": "non_interactive"},
    )
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        return _audit_response(
            [tool_call.id],
            deny=True if deny is _MISSING else deny,
            score=0,
            include_deny=include_deny,
        )

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)
    cfg = _audit_config(tmp_path, protect_system_directories=False)

    async with audit_database() as db:
        result = await _audit_round(db, cfg=cfg, tool_calls=[tool_call], working_directory=tmp_path)

    assert result is not None
    assert result.status is AuditRecordStatus.PASSED
    assert result.may_execute is True
    assert result.confirmation_payload is None
    assert result.tool_results == ()
    assert len(generate_calls) == 1
    assert generate_calls[0]["messages"][0].content == AUDIT_STANDARD_BATCH_PROMPT
    request_payload = json.loads(generate_calls[0]["messages"][1].content)
    assert request_payload["directory_policy"] == {"allowed_operation_dirs": [str(tmp_path)]}
    assert protected_file.read_text(encoding="utf-8") == "sentinel"


def _file_tool_call(operation: str, path: str) -> InternalToolCall:
    arguments: dict[str, Any] = {"operation": operation, "path": path}
    if operation == "write":
        arguments["content"] = "changed"
    elif operation == "edit":
        arguments.update(old_text="original", new_text="changed")
    else:
        arguments["patch"] = "@@\n-original\n+changed"
    return InternalToolCall(id=f"file-{operation}-call", name="file_tool", arguments=arguments)


@pytest.mark.parametrize("operation", ["write", "edit", "patch"])
@pytest.mark.asyncio
async def test_system_protected_directory_blocks_file_mutations_before_model_call(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    audit_root = tmp_path / "audit"
    _patch_audit_root(monkeypatch, audit_root)
    protected_file = tmp_path / "data" / "skills" / "protected.txt"
    protected_file.parent.mkdir(parents=True)
    protected_file.write_text("original", encoding="utf-8")
    generate_calls = 0

    async def unexpected_generate(**_kwargs: Any) -> InternalResponse:
        nonlocal generate_calls
        generate_calls += 1
        raise AssertionError("locally protected file mutation must not call the audit model")

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", unexpected_generate)
    tool_call = _file_tool_call(operation, "data/skills/protected.txt")

    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=_audit_config(tmp_path, threshold=0),
            tool_calls=[tool_call],
            working_directory=tmp_path,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.BLOCKED
    assert result.may_execute is False
    assert result.confirmation_payload is None
    assert generate_calls == 0
    assert json.loads(result.tool_results[0].content or "{}")["status"] == AuditRecordStatus.BLOCKED.value
    assert protected_file.read_text(encoding="utf-8") == "original"

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
        assert record.context_file_path is not None and Path(record.context_file_path).is_file()
        assert len(details) == 1
        assert details[0].conclusion is AuditToolConclusion.BLOCKED
        assert details[0].score == 10
        assert claims == []
        assert executions == []
        assert cards == []


@pytest.mark.parametrize("operation", ["write", "edit", "patch"])
@pytest.mark.asyncio
async def test_disabled_system_protection_audits_file_mutations_with_model(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")
    protected_file = tmp_path / "data" / "skills" / "protected.txt"
    protected_file.parent.mkdir(parents=True)
    protected_file.write_text("original", encoding="utf-8")
    tool_call = _file_tool_call(operation, "data/skills/protected.txt")
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        return _audit_response([tool_call.id], score=0, include_deny=False)

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)

    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=_audit_config(tmp_path, protect_system_directories=False),
            tool_calls=[tool_call],
            working_directory=tmp_path,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.PASSED
    assert result.may_execute is True
    assert result.confirmation_payload is None
    assert len(generate_calls) == 1
    assert generate_calls[0]["messages"][0].content == AUDIT_STANDARD_BATCH_PROMPT
    request_payload = json.loads(generate_calls[0]["messages"][1].content)
    assert request_payload["directory_policy"] == {"allowed_operation_dirs": [str(tmp_path)]}
    assert protected_file.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_disabled_system_protection_keeps_high_risk_confirmation_and_summary_policy(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")
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
            return _audit_response([tool_call.id], deny=True, score=9)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="High-risk action requires confirmation"),
            model=AUDIT_MODEL_ID,
            usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            finish_reason="stop",
        )

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)

    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=_audit_config(tmp_path, protect_system_directories=False),
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
    assert generate_calls[1]["messages"][0].content == AUDIT_SUMMARY_PROMPT.format(audit_report_language="zh")
    summary_payload = json.loads(generate_calls[1]["messages"][1].content)
    assert summary_payload["directory_policy"] == {"allowed_operation_dirs": [str(tmp_path)]}
    assert "write_protected_directories" not in summary_payload["directory_policy"]
    assert protected_file.read_text(encoding="utf-8") == "sentinel"

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)

        assert record is not None
        assert record.status is AuditRecordStatus.PENDING
        assert record.context_file_path is not None and Path(record.context_file_path).is_file()
        assert len(details) == 1
        assert details[0].conclusion is AuditToolConclusion.PENDING
        assert details[0].score == 9


@pytest.mark.parametrize("operation", ["write", "edit", "patch"])
@pytest.mark.asyncio
async def test_allowed_operation_directory_is_not_write_protected(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    audit_root = tmp_path / "audit"
    _patch_audit_root(monkeypatch, audit_root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    allowed_directory = tmp_path / "shared"
    allowed_directory.mkdir()
    allowed_file = allowed_directory / "allowed.txt"
    allowed_file.write_text("original", encoding="utf-8")
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        return _audit_response([f"file-{operation}-call"], deny=False, score=0)

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)
    tool_call = _file_tool_call(operation, str(allowed_file))

    async with audit_database() as db:
        result = await _audit_round(
            db,
            cfg=_audit_config(
                tmp_path,
                threshold=0,
                allowed_operation_dirs=[str(allowed_directory)],
            ),
            tool_calls=[tool_call],
            working_directory=workspace,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.PASSED
    assert result.may_execute is True
    assert result.confirmation_payload is None
    assert result.tool_results == ()
    assert len(generate_calls) == 1
    assert allowed_file.read_text(encoding="utf-8") == "original"

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        claims = list((await db.execute(select(AuditConfirmationClaim).where(AuditConfirmationClaim.audit_record_id == result.audit_record_id))).scalars().all())
        executions = list((await db.execute(select(AuditExecutionRecord).where(AuditExecutionRecord.audit_record_id == result.audit_record_id))).scalars().all())

        assert record is not None and record.status is AuditRecordStatus.PASSED
        assert len(details) == 1
        assert details[0].conclusion is AuditToolConclusion.PASSED
        assert details[0].score == 0
        assert claims == []
        assert executions == []


@pytest.mark.parametrize(
    ("include_deny", "deny"),
    [(False, _MISSING), (True, "false")],
    ids=["missing-deny", "non-bool-deny"],
)
@pytest.mark.asyncio
async def test_missing_or_non_boolean_deny_fails_closed_without_execution(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    include_deny: bool,
    deny: Any,
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
            cfg=_audit_config(tmp_path, threshold=0),
            tool_calls=[tool_call],
            working_directory=tmp_path,
        )

    assert result is not None
    assert result.status is AuditRecordStatus.AUDIT_FAILED
    assert result.may_execute is False
    assert result.confirmation_payload is None
    assert len(generate_calls) == 1
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
