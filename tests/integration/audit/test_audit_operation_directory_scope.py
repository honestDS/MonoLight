import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import select

import app.core.audit.service as audit_service_module
import app.core.confirmation.service as confirmation_service_module
from app.core import paths
from app.core.crud.audit.audit import audit_crud
from app.core.prompts import AUDIT_STANDARD_BATCH_PROMPT
from app.core.terminal.schemas import TerminalSessionStatus
from app.models.audit import (
    AuditConfirmationClaim,
    AuditExecutionRecord,
    AuditRecordStatus,
    AuditToolConclusion,
)
from app.models.channel import ModelChannel
from app.models.message import InternalResponse, InternalToolCall, Message, MessageRole, MessageType
from app.models.terminal_session import TerminalControlCommand, TerminalSession
from tests.integration.audit import test_audit_directory_policy as directory_policy_tests
from tests.integration.audit.test_audit_directory_policy import (
    SESSION_ID,
    TERMINAL_SESSION_ID,
    UID,
    _audit_config,
    _audit_response,
    _audit_round,
    _patch_audit_root,
    _seed_audit_channel,
    _seed_terminal_history,
)

isolate_system_protection_root = directory_policy_tests.isolate_system_protection_root
audit_database = directory_policy_tests.audit_database


@pytest.mark.parametrize(
    ("tool_name", "execution_mode"),
    [
        ("file_tool", None),
        ("execute_shell", "non_interactive"),
        ("execute_shell", "interactive"),
        ("terminal_tool", None),
    ],
    ids=["file-read", "shell-non-interactive", "shell-interactive", "terminal-write"],
)
@pytest.mark.parametrize("source_kind", ["outside", "protected"], ids=["outside", "protected"])
@pytest.mark.parametrize(
    "protect_system_directories",
    [True, False],
    ids=["protection-enabled", "protection-disabled"],
)
@pytest.mark.parametrize("threshold", [0, 5], ids=["threshold-0", "threshold-5"])
@pytest.mark.asyncio
async def test_read_only_audit_passes_outside_write_policy(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tool_name: str,
    execution_mode: str | None,
    source_kind: str,
    protect_system_directories: bool,
    threshold: int,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_file = tmp_path / "outside" / "ordinary.txt" if source_kind == "outside" else tmp_path / "data" / "skills" / "ordinary.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("sentinel", encoding="utf-8")
    command = f'type "{source_file}"'
    if tool_name == "file_tool":
        arguments = {
            "operation": "read",
            "path": str(source_file),
            "start_line": 1,
            "end_line": 5,
        }
    elif tool_name == "terminal_tool":
        await _seed_terminal_history(audit_database, workspace)
        arguments = {"operation": "write", "terminal_session_id": TERMINAL_SESSION_ID, "data": f"{command}\n"}
    else:
        arguments = {"command": command, "execution_mode": execution_mode}

    tool_call = InternalToolCall(id="readonly-audit-call", name=tool_name, arguments=arguments)
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        return _audit_response([tool_call.id], deny=False, score=0)

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)
    cfg = _audit_config(
        tmp_path,
        threshold=threshold,
        allowed_operation_dirs=[str(workspace)],
        protect_system_directories=protect_system_directories,
    )

    async with audit_database() as db:
        result = await _audit_round(db, cfg=cfg, tool_calls=[tool_call], working_directory=workspace)

    assert result is not None
    assert result.status is AuditRecordStatus.PASSED
    assert result.may_execute is True
    assert result.confirmation_payload is None
    assert result.tool_results == ()
    assert len(generate_calls) == 1
    request_payload = json.loads(generate_calls[0]["messages"][1].content)
    directory_policy = request_payload["directory_policy"]
    assert directory_policy["allowed_operation_dirs"] == [str(workspace)]
    assert workspace.resolve() not in source_file.resolve().parents
    if protect_system_directories:
        assert str(tmp_path / "data" / "skills") in directory_policy["write_protected_directories"]
    else:
        assert "write_protected_directories" not in directory_policy
    assert source_file.read_text(encoding="utf-8") == "sentinel"

    async with audit_database() as db:
        record = await audit_crud.get_record(db, result.audit_record_id)
        details = await audit_crud.list_tool_details(db, result.audit_record_id)
        assert record is not None
        assert record.status is AuditRecordStatus.PASSED
        assert len(details) == 1
        assert details[0].conclusion is AuditToolConclusion.PASSED
        assert details[0].score == 0
        if tool_name == "terminal_tool":
            terminal_session = await db.get(TerminalSession, TERMINAL_SESSION_ID)
            terminal_commands = list((await db.execute(select(TerminalControlCommand).where(TerminalControlCommand.terminal_session_id == TERMINAL_SESSION_ID))).scalars().all())
            assert terminal_session is not None
            assert terminal_session.status is TerminalSessionStatus.RUNNING
            assert len(terminal_commands) == 2


@pytest.mark.asyncio
async def test_disabled_system_protection_allows_permitted_shell_with_boolean_deny(
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
        id="unprotected-shell-call",
        name="execute_shell",
        arguments={"command": f'echo changed > "{protected_file}"', "execution_mode": "non_interactive"},
    )
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        return _audit_response([tool_call.id], deny=False, score=0)

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
        return _audit_response([tool_call.id], deny=False, score=0)

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
    "config_case",
    ["empty", "extra", "normalized_duplicates"],
    ids=["empty", "extra", "normalized-duplicates"],
)
@pytest.mark.parametrize(
    "protect_system_directories",
    [True, False],
    ids=["protection-enabled", "protection-disabled"],
)
@pytest.mark.asyncio
async def test_user_temp_directory_policy_is_scoped_without_mutating_config(
    audit_database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_case: str,
    protect_system_directories: bool,
) -> None:
    await _seed_audit_channel(audit_database)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
    _patch_audit_root(monkeypatch, tmp_path / "audit")

    shared = tmp_path / "shared"
    shared.mkdir()
    if config_case == "empty":
        configured_allowed_dirs = []
        expected_explicit_dirs = []
    elif config_case == "extra":
        configured_allowed_dirs = [str(shared)]
        expected_explicit_dirs = [str(shared.resolve())]
    else:
        configured_allowed_dirs = [str(shared / ".." / "shared"), str(shared), "relative-not-allowed"]
        expected_explicit_dirs = [str(shared.resolve())]

    cfg = _audit_config(
        tmp_path,
        allowed_operation_dirs=configured_allowed_dirs,
        protect_system_directories=protect_system_directories,
    )
    original_allowed_dirs = list(cfg.tool.allowed_operation_dirs)
    assert original_allowed_dirs == configured_allowed_dirs
    assert cfg.security.protect_system_directories is protect_system_directories

    user_ids = (UID, f"{UID}-other")
    user_temp_dirs = {uid: paths.get_user_temp_dir(tmp_path, uid) for uid in user_ids}
    generate_calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> InternalResponse:
        generate_calls.append(kwargs)
        request_payload = json.loads(kwargs["messages"][1].content)
        tool_call_ids = [item["tool_call_id"] for item in request_payload["tool_calls"]]
        return _audit_response(tool_call_ids, deny=False, score=0)

    monkeypatch.setattr(audit_service_module.LLMClient, "generate", generate)

    for index, uid in enumerate(user_ids, start=1):
        user_temp_dir = user_temp_dirs[uid]
        note_path = user_temp_dir / "note.txt"
        tool_call = InternalToolCall(
            id=f"user-temp-shell-call-{index}",
            name="execute_shell",
            arguments={"command": f'echo changed > "{note_path}"', "execution_mode": "non_interactive"},
        )

        async with audit_database() as db:
            result = await confirmation_service_module.prepare_tool_round(
                db,
                cfg=cfg,
                tool_calls=[tool_call],
                source_assistant_message_id=100 + index,
                uid=uid,
                operator_username="tester",
                session_id=f"{SESSION_ID}-{index}",
                source="integration",
                language="zh",
                execution_scope="foreground",
            )

        assert result is not None
        assert result.status is AuditRecordStatus.PASSED
        assert result.may_execute is True
        assert result.confirmation_payload is None
        assert len(generate_calls) == index

        model_request = json.loads(generate_calls[-1]["messages"][1].content)
        assert model_request["working_directory"] == str(user_temp_dir)
        policy_dirs = model_request["directory_policy"]["allowed_operation_dirs"]
        expected_policy_dirs = [*expected_explicit_dirs, str(user_temp_dir)]
        assert policy_dirs == expected_policy_dirs
        other_uid = user_ids[1] if uid == user_ids[0] else user_ids[0]
        assert str(tmp_path / "temp") not in policy_dirs
        assert str(user_temp_dirs[other_uid]) not in policy_dirs
        assert len(policy_dirs) == len(set(policy_dirs))
        assert not note_path.exists()

        async with audit_database() as db:
            record = await audit_crud.get_record(db, result.audit_record_id)
            assert record is not None
            assert record.uid == uid
            assert record.working_directory == str(user_temp_dir)
            assert record.status is AuditRecordStatus.PASSED
            assert record.context_file_path is not None
            context_path = Path(record.context_file_path)
            assert context_path.is_file()
            persisted_context = json.loads(context_path.read_text(encoding="utf-8"))
            scoring_user_messages = [message for message in persisted_context["scoring_request"]["messages"] if message["role"] == MessageRole.USER.value]
            assert len(scoring_user_messages) == 1
            persisted_request = json.loads(scoring_user_messages[0]["content"])
            assert persisted_request["directory_policy"] == model_request["directory_policy"]

    assert cfg.tool.allowed_operation_dirs == original_allowed_dirs
