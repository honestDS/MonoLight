import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.core.terminal.manager as terminal_manager_module
from app.core.crud.audit import audit_crud
from app.core.terminal.manager import _TerminalSessionRuntime, cleanup_terminal_sessions_by_chat_session
from app.core.terminal.schemas import TerminalOutputBufferState, TerminalSessionStatus
from app.models.audit import (
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolConclusion,
    AuditToolDetail,
)
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession

WORKER_ID = "terminal-test-worker"
CLAIM_TOKEN = "terminal-test-claim"


@pytest.fixture
async def terminal_audit_database(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'terminal-audit.db'}",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _output_buffer() -> TerminalOutputBufferState:
    return TerminalOutputBufferState(
        capacity_bytes=1024,
        oldest_offset=0,
        next_offset=0,
        oldest_sequence=1,
        next_sequence=1,
    )


async def _seed_audit_execution(session_factory, tmp_path, *, execution_count=1):
    async with session_factory() as db:
        audit_record = AuditRecord(
            uid="user-1",
            operator_username="tester",
            session_id="chat-session-1",
            source="web",
            language="en",
            status=AuditRecordStatus.EXECUTING,
            source_assistant_message_id=1,
            working_directory=str(tmp_path),
            round_arguments_hash="a" * 64,
            tool_count=execution_count,
            execution_claim_token=CLAIM_TOKEN,
        )
        db.add(audit_record)
        await db.flush()

        runtimes = []
        for index in range(execution_count):
            detail = AuditToolDetail(
                audit_record_id=audit_record.id,
                original_tool_call_id=f"original-call-{index}",
                turn_index=index,
                tool_name="execute_shell",
                conclusion=AuditToolConclusion.PASSED,
                score=10,
                reason="test",
                arguments_hash="b" * 64,
                arguments_summary="{}",
                file_snapshots=[],
            )
            db.add(detail)
            await db.flush()

            execution = AuditExecutionRecord(
                audit_record_id=audit_record.id,
                audit_tool_detail_id=detail.id,
                attempt_no=1,
                status=AuditExecutionStatus.RUNNING,
                claim_token=CLAIM_TOKEN,
                execution_node="test-node",
                new_tool_call_id=f"new-call-{index}",
            )
            db.add(execution)
            await db.flush()

            terminal_session = TerminalSession(
                terminal_session_id=f"t{index}" + "s" * 31,
                uid="user-1",
                session_id="chat-session-1",
                original_tool_call_id=f"original-call-{index}",
                profile_id=1,
                audit_record_id=audit_record.id,
                audit_execution_record_id=execution.id,
                command="python -i",
                working_directory=str(tmp_path),
                status=TerminalSessionStatus.RUNNING,
                allowed_actions=["status"],
                locked_by=WORKER_ID,
                lock_until=int(time.time()) + 3600,
            )
            db.add(terminal_session)
            runtimes.append((execution.id, terminal_session))

        await db.commit()
        return audit_record.id, runtimes


@pytest.mark.parametrize(
    ("terminal_status", "exit_code", "failure_reason", "execution_status", "round_status"),
    [
        (
            TerminalSessionStatus.EXITED,
            0,
            None,
            AuditExecutionStatus.SUCCEEDED,
            AuditRecordStatus.SUCCEEDED,
        ),
        (
            TerminalSessionStatus.EXITED,
            7,
            None,
            AuditExecutionStatus.FAILED,
            AuditRecordStatus.FAILED,
        ),
        (
            TerminalSessionStatus.FAILED,
            None,
            "driver failed",
            AuditExecutionStatus.FAILED,
            AuditRecordStatus.FAILED,
        ),
        (
            TerminalSessionStatus.LOST,
            None,
            "worker lease lost",
            AuditExecutionStatus.EXECUTION_UNKNOWN,
            AuditRecordStatus.EXECUTION_UNKNOWN,
        ),
    ],
)
@pytest.mark.asyncio
async def test_runtime_snapshot_finishes_terminal_audit_and_projects_completed_round(
    terminal_audit_database,
    tmp_path,
    monkeypatch,
    terminal_status,
    exit_code,
    failure_reason,
    execution_status,
    round_status,
):
    confirmation_calls = []

    async def record_confirmation_call(_db, *, audit_record_id):
        confirmation_calls.append(audit_record_id)

    monkeypatch.setattr(terminal_manager_module, "AsyncSessionLocal", terminal_audit_database)
    monkeypatch.setattr(terminal_manager_module, "_update_terminal_confirmation_status", record_confirmation_call)
    audit_record_id, runtimes = await _seed_audit_execution(terminal_audit_database, tmp_path)
    execution_id, terminal_session = runtimes[0]
    runtime = _TerminalSessionRuntime(terminal_session, WORKER_ID)

    await runtime._update_runtime_snapshot(
        terminal_status,
        output_buffer=_output_buffer(),
        exit_code=exit_code,
        failure_reason=failure_reason,
    )

    async with terminal_audit_database() as db:
        stored_terminal = await db.get(TerminalSession, terminal_session.terminal_session_id)
        stored_execution = await db.get(AuditExecutionRecord, execution_id)
        stored_record = await db.get(AuditRecord, audit_record_id)

    assert stored_terminal is not None
    assert stored_terminal.status is terminal_status
    assert stored_terminal.exit_code == exit_code
    assert stored_terminal.failure_reason == failure_reason
    assert stored_execution is not None
    assert stored_execution.status is execution_status
    assert stored_execution.result_summary is not None
    assert json.loads(stored_execution.result_summary) == {
        "terminal_session_id": terminal_session.terminal_session_id,
        "status": terminal_status.value,
        "exit_code": exit_code,
        "failure_reason": failure_reason,
    }
    if execution_status is AuditExecutionStatus.SUCCEEDED:
        assert stored_execution.error is None
    else:
        assert stored_execution.error == stored_execution.result_summary
    assert stored_record is not None
    assert stored_record.status is round_status
    assert stored_record.execution_claim_token is None
    assert confirmation_calls == [audit_record_id]


@pytest.mark.asyncio
async def test_runtime_snapshot_finishes_unaudited_terminal_without_audit_side_effects(
    terminal_audit_database,
    tmp_path,
    monkeypatch,
):
    async def fail_audit_call(*args, **kwargs):
        raise AssertionError("unaudited terminal finalization must not call audit services")

    monkeypatch.setattr(terminal_manager_module, "AsyncSessionLocal", terminal_audit_database)
    for method_name in (
        "get_execution_record",
        "get_record",
        "finish_execution_attempt",
        "finish_execution_round_if_complete",
    ):
        monkeypatch.setattr(terminal_manager_module.audit_crud, method_name, fail_audit_call)
    monkeypatch.setattr(terminal_manager_module, "_update_terminal_confirmation_status", fail_audit_call)

    terminal_session = TerminalSession(
        terminal_session_id="unaudited-terminal" + "s" * 19,
        uid="user-1",
        session_id="chat-session-1",
        original_tool_call_id="tool-call-1",
        profile_id=1,
        audit_record_id=None,
        audit_execution_record_id=None,
        command="python -i",
        working_directory=str(tmp_path),
        status=TerminalSessionStatus.RUNNING,
        allowed_actions=["status"],
        locked_by=WORKER_ID,
        lock_until=int(time.time()) + 3600,
    )
    async with terminal_audit_database() as db:
        db.add(terminal_session)
        await db.commit()

    runtime = _TerminalSessionRuntime(terminal_session, WORKER_ID)
    await runtime._update_runtime_snapshot(
        TerminalSessionStatus.EXITED,
        output_buffer=_output_buffer(),
        exit_code=7,
    )

    async with terminal_audit_database() as db:
        stored_terminal = await db.get(TerminalSession, terminal_session.terminal_session_id)

    assert stored_terminal is not None
    assert stored_terminal.status is TerminalSessionStatus.EXITED
    assert stored_terminal.exit_code == 7
    assert stored_terminal.audit_record_id is None
    assert stored_terminal.audit_execution_record_id is None


@pytest.mark.asyncio
async def test_runtime_snapshot_projects_confirmation_only_after_the_whole_round_finishes(
    terminal_audit_database,
    tmp_path,
    monkeypatch,
):
    confirmation_calls = []

    async def record_confirmation_call(_db, *, audit_record_id):
        confirmation_calls.append(audit_record_id)

    monkeypatch.setattr(terminal_manager_module, "AsyncSessionLocal", terminal_audit_database)
    monkeypatch.setattr(terminal_manager_module, "_update_terminal_confirmation_status", record_confirmation_call)
    audit_record_id, runtimes = await _seed_audit_execution(terminal_audit_database, tmp_path, execution_count=2)

    first_execution_id, first_terminal = runtimes[0]
    second_execution_id, second_terminal = runtimes[1]
    await _TerminalSessionRuntime(first_terminal, WORKER_ID)._update_runtime_snapshot(
        TerminalSessionStatus.EXITED,
        output_buffer=_output_buffer(),
        exit_code=0,
    )

    assert confirmation_calls == []
    async with terminal_audit_database() as db:
        first_execution = await db.get(AuditExecutionRecord, first_execution_id)
        second_execution = await db.get(AuditExecutionRecord, second_execution_id)
        stored_record = await db.get(AuditRecord, audit_record_id)
    assert first_execution is not None and first_execution.status is AuditExecutionStatus.SUCCEEDED
    assert second_execution is not None and second_execution.status is AuditExecutionStatus.RUNNING
    assert stored_record is not None and stored_record.status is AuditRecordStatus.EXECUTING

    await _TerminalSessionRuntime(second_terminal, WORKER_ID)._update_runtime_snapshot(
        TerminalSessionStatus.EXITED,
        output_buffer=_output_buffer(),
        exit_code=0,
    )

    assert confirmation_calls == [audit_record_id]
    async with terminal_audit_database() as db:
        stored_record = await db.get(AuditRecord, audit_record_id)
    assert stored_record is not None and stored_record.status is AuditRecordStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_runtime_snapshot_rolls_back_terminal_and_execution_when_audit_finish_fails(
    terminal_audit_database,
    tmp_path,
    monkeypatch,
):
    confirmation_calls = []

    async def record_confirmation_call(_db, *, audit_record_id):
        confirmation_calls.append(audit_record_id)

    monkeypatch.setattr(terminal_manager_module, "AsyncSessionLocal", terminal_audit_database)
    monkeypatch.setattr(terminal_manager_module, "_update_terminal_confirmation_status", record_confirmation_call)
    audit_record_id, runtimes = await _seed_audit_execution(terminal_audit_database, tmp_path)
    execution_id, terminal_session = runtimes[0]
    runtime = _TerminalSessionRuntime(terminal_session, WORKER_ID)
    original_finish = audit_crud.finish_execution_attempt

    async def fail_finish(*_args, **_kwargs):
        raise RuntimeError("audit finish failed")

    monkeypatch.setattr(terminal_manager_module.audit_crud, "finish_execution_attempt", fail_finish)
    with pytest.raises(RuntimeError, match="audit finish failed"):
        await runtime._update_runtime_snapshot(
            TerminalSessionStatus.EXITED,
            output_buffer=_output_buffer(),
            exit_code=0,
        )

    async with terminal_audit_database() as db:
        stored_terminal = await db.get(TerminalSession, terminal_session.terminal_session_id)
        stored_execution = await db.get(AuditExecutionRecord, execution_id)
        stored_record = await db.get(AuditRecord, audit_record_id)
    assert stored_terminal is not None and stored_terminal.status is TerminalSessionStatus.RUNNING
    assert stored_execution is not None and stored_execution.status is AuditExecutionStatus.RUNNING
    assert stored_record is not None
    assert stored_record.status is AuditRecordStatus.EXECUTING
    assert stored_record.execution_claim_token == CLAIM_TOKEN
    assert confirmation_calls == []

    monkeypatch.setattr(terminal_manager_module.audit_crud, "finish_execution_attempt", original_finish)
    await runtime._update_runtime_snapshot(
        TerminalSessionStatus.EXITED,
        output_buffer=_output_buffer(),
        exit_code=0,
    )

    async with terminal_audit_database() as db:
        stored_terminal = await db.get(TerminalSession, terminal_session.terminal_session_id)
        stored_execution = await db.get(AuditExecutionRecord, execution_id)
        stored_record = await db.get(AuditRecord, audit_record_id)
    assert stored_terminal is not None and stored_terminal.status is TerminalSessionStatus.EXITED
    assert stored_execution is not None and stored_execution.status is AuditExecutionStatus.SUCCEEDED
    assert stored_record is not None and stored_record.status is AuditRecordStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_cleanup_terminal_sessions_by_chat_session_finalizes_audit_and_deletes_commands(
    terminal_audit_database,
    tmp_path,
    monkeypatch,
):
    async def cleanup_process_identity(_identity):
        return SimpleNamespace(errors=())

    async def fail_confirmation_projection(*args, **kwargs):
        raise AssertionError("terminal cleanup must not project confirmation messages")

    monkeypatch.setattr(terminal_manager_module, "cleanup_terminal_process_identity", cleanup_process_identity)
    monkeypatch.setattr(terminal_manager_module, "_update_terminal_confirmation_status", fail_confirmation_projection)
    audit_record_id, runtimes = await _seed_audit_execution(terminal_audit_database, tmp_path)
    execution_id, terminal_session = runtimes[0]
    control_command = TerminalControlCommand(
        terminal_session_id=terminal_session.terminal_session_id,
        request_id="cleanup-request",
        action="close",
        payload={"force": True},
        payload_hash="c" * 64,
        status=TerminalControlCommandStatus.PENDING,
    )
    async with terminal_audit_database() as db:
        db.add(control_command)
        await db.commit()
    assert control_command.id is not None

    async with terminal_audit_database() as db:
        deleted_count = await cleanup_terminal_sessions_by_chat_session(
            db,
            session_id="chat-session-1",
            uid="user-1",
        )
        await db.commit()
        stored_execution = await db.get(AuditExecutionRecord, execution_id)
        stored_record = await db.get(AuditRecord, audit_record_id)
        assert await db.get(TerminalSession, terminal_session.terminal_session_id) is None
        assert await db.get(TerminalControlCommand, control_command.id) is None

        repeated_count = await cleanup_terminal_sessions_by_chat_session(
            db,
            session_id="chat-session-1",
            uid="user-1",
        )
        await db.commit()
        repeated_execution = await db.get(AuditExecutionRecord, execution_id)
        repeated_record = await db.get(AuditRecord, audit_record_id)

    assert deleted_count == 1
    assert repeated_count == 0
    assert stored_execution is not None
    assert stored_execution.status is AuditExecutionStatus.EXECUTION_UNKNOWN
    assert stored_record is not None
    assert stored_record.status is AuditRecordStatus.EXECUTION_UNKNOWN
    assert stored_record.execution_claim_token is None
    assert repeated_execution is not None
    assert repeated_execution.status is AuditExecutionStatus.EXECUTION_UNKNOWN
    assert repeated_record is not None
    assert repeated_record.status is AuditRecordStatus.EXECUTION_UNKNOWN
    assert repeated_record.execution_claim_token is None
    assert stored_record.execution_claim_token is None


@pytest.mark.asyncio
async def test_runtime_snapshot_finalization_is_idempotent(terminal_audit_database, tmp_path, monkeypatch, caplog):
    finish_calls = 0
    update_snapshot_calls = 0
    confirmation_calls = []
    original_finish = audit_crud.finish_execution_attempt
    original_update_snapshot = terminal_manager_module.terminal_session_crud.update_runtime_snapshot

    async def count_finish(*args, **kwargs):
        nonlocal finish_calls
        finish_calls += 1
        return await original_finish(*args, **kwargs)

    async def record_confirmation_call(_db, *, audit_record_id):
        confirmation_calls.append(audit_record_id)

    async def count_update_snapshot(*args, **kwargs):
        nonlocal update_snapshot_calls
        update_snapshot_calls += 1
        return await original_update_snapshot(*args, **kwargs)

    monkeypatch.setattr(terminal_manager_module, "AsyncSessionLocal", terminal_audit_database)
    monkeypatch.setattr(terminal_manager_module.audit_crud, "finish_execution_attempt", count_finish)
    monkeypatch.setattr(terminal_manager_module.terminal_session_crud, "update_runtime_snapshot", count_update_snapshot)
    monkeypatch.setattr(terminal_manager_module, "_update_terminal_confirmation_status", record_confirmation_call)
    audit_record_id, runtimes = await _seed_audit_execution(terminal_audit_database, tmp_path)
    execution_id, terminal_session = runtimes[0]
    runtime = _TerminalSessionRuntime(terminal_session, WORKER_ID)

    for _ in range(2):
        await runtime._update_runtime_snapshot(
            TerminalSessionStatus.EXITED,
            output_buffer=_output_buffer(),
            exit_code=0,
        )

    async with terminal_audit_database() as db:
        stored_terminal = await db.get(TerminalSession, terminal_session.terminal_session_id)
        stored_execution = await db.get(AuditExecutionRecord, execution_id)
        stored_record = await db.get(AuditRecord, audit_record_id)
    assert finish_calls == 1
    assert update_snapshot_calls == 1
    assert confirmation_calls == [audit_record_id]
    assert "Terminal session audit execution is already finalized" not in caplog.text
    assert stored_terminal is not None and stored_terminal.status is TerminalSessionStatus.EXITED
    assert stored_execution is not None and stored_execution.status is AuditExecutionStatus.SUCCEEDED
    assert stored_record is not None and stored_record.status is AuditRecordStatus.SUCCEEDED
