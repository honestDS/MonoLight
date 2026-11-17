import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from sqlmodel import select

import app.core.audit.confirmation_events as confirmation_events_module
import app.core.terminal.audit_lifecycle as terminal_audit_lifecycle_module
import app.core.terminal.session_runtime_commands as terminal_runtime_commands_module
import app.core.terminal.session_runtime_core as terminal_runtime_core_module
import app.core.terminal.session_runtime_state as terminal_runtime_state_module
import app.core.terminal.worker_coordinator as terminal_worker_module
from app.core.audit.confirmation_lifecycle import stop_interactive_reply
from app.core.audit.storage import write_audit_json
from app.core.crud.audit.audit import audit_crud
from app.core.exceptions import ServerException
from app.core.terminal.manager import cleanup_terminal_sessions_by_chat_session
from app.core.terminal.schemas import TerminalOutputBufferState, TerminalSessionStatus
from app.core.terminal.session_runtime import _TerminalSessionRuntime
from app.models.audit import (
    AuditExecutionRecord,
    AuditExecutionStatus,
    AuditRecord,
    AuditRecordStatus,
    AuditToolConclusion,
    AuditToolDetail,
    AuditToolResultVersion,
)
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession


def _patch_terminal_session_factory(monkeypatch: pytest.MonkeyPatch, session_factory) -> None:
    for module in (
        terminal_runtime_commands_module,
        terminal_runtime_core_module,
        terminal_runtime_state_module,
        terminal_worker_module,
    ):
        monkeypatch.setattr(module, "AsyncSessionLocal", session_factory)


def _patch_confirmation_projection(monkeypatch: pytest.MonkeyPatch, callback) -> None:
    monkeypatch.setattr(terminal_runtime_state_module, "_update_terminal_confirmation_status", callback)
    monkeypatch.setattr(terminal_worker_module, "_update_terminal_confirmation_status", callback)


WORKER_ID = "terminal-test-worker"
CLAIM_TOKEN = "terminal-test-claim"


@pytest.fixture
async def terminal_audit_database(database_factory):
    async with database_factory(name="terminal-audit.db") as factory:
        yield factory


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

    _patch_terminal_session_factory(monkeypatch, terminal_audit_database)
    _patch_confirmation_projection(monkeypatch, record_confirmation_call)
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

    _patch_terminal_session_factory(monkeypatch, terminal_audit_database)
    for method_name in (
        "get_execution_record",
        "get_record",
        "finish_execution_attempt",
        "finish_execution_round_if_complete",
    ):
        monkeypatch.setattr(terminal_audit_lifecycle_module.audit_crud, method_name, fail_audit_call)
    _patch_confirmation_projection(monkeypatch, fail_audit_call)

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

    _patch_terminal_session_factory(monkeypatch, terminal_audit_database)
    _patch_confirmation_projection(monkeypatch, record_confirmation_call)
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

    _patch_terminal_session_factory(monkeypatch, terminal_audit_database)
    _patch_confirmation_projection(monkeypatch, record_confirmation_call)
    audit_record_id, runtimes = await _seed_audit_execution(terminal_audit_database, tmp_path)
    execution_id, terminal_session = runtimes[0]
    runtime = _TerminalSessionRuntime(terminal_session, WORKER_ID)
    original_finish = audit_crud.finish_execution_attempt

    async def fail_finish(*_args, **_kwargs):
        raise RuntimeError("audit finish failed")

    monkeypatch.setattr(terminal_audit_lifecycle_module.audit_crud, "finish_execution_attempt", fail_finish)
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

    monkeypatch.setattr(terminal_audit_lifecycle_module.audit_crud, "finish_execution_attempt", original_finish)
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

    monkeypatch.setattr(terminal_audit_lifecycle_module, "cleanup_terminal_process_identity", cleanup_process_identity)
    _patch_confirmation_projection(monkeypatch, fail_confirmation_projection)
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
async def test_cleanup_terminal_sessions_by_chat_session_preserves_data_when_process_cleanup_fails(
    terminal_audit_database,
    tmp_path,
    monkeypatch,
):
    async def fail_process_cleanup(_identity):
        return SimpleNamespace(errors=("process did not terminate pid 123",))

    monkeypatch.setattr(terminal_audit_lifecycle_module, "cleanup_terminal_process_identity", fail_process_cleanup)
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
        with pytest.raises(ServerException):
            await cleanup_terminal_sessions_by_chat_session(
                db,
                session_id="chat-session-1",
                uid="user-1",
            )
        await db.rollback()
        stored_terminal = await db.get(TerminalSession, terminal_session.terminal_session_id)
        stored_command = await db.get(TerminalControlCommand, control_command.id)
        stored_execution = await db.get(AuditExecutionRecord, execution_id)
        stored_record = await db.get(AuditRecord, audit_record_id)

    assert stored_terminal is not None
    assert stored_command is not None
    assert stored_execution is not None and stored_execution.status is AuditExecutionStatus.RUNNING
    assert stored_record is not None
    assert stored_record.status is AuditRecordStatus.EXECUTING
    assert stored_record.execution_claim_token == CLAIM_TOKEN


@pytest.mark.asyncio
async def test_runtime_snapshot_finalization_is_idempotent(terminal_audit_database, tmp_path, monkeypatch, caplog):
    finish_calls = 0
    update_snapshot_calls = 0
    confirmation_calls = []
    original_finish = audit_crud.finish_execution_attempt
    original_update_snapshot = terminal_runtime_state_module.terminal_session_crud.update_runtime_snapshot

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

    _patch_terminal_session_factory(monkeypatch, terminal_audit_database)
    monkeypatch.setattr(terminal_audit_lifecycle_module.audit_crud, "finish_execution_attempt", count_finish)
    monkeypatch.setattr(terminal_runtime_state_module.terminal_session_crud, "update_runtime_snapshot", count_update_snapshot)
    _patch_confirmation_projection(monkeypatch, record_confirmation_call)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_lifecycle", ["running", "exited"], ids=["running", "exited"])
async def test_stop_interactive_reply_deletes_terminal_index_and_preserves_audit_history(
    terminal_audit_database,
    tmp_path,
    monkeypatch,
    terminal_lifecycle,
):
    notifications = []

    async def cleanup_process_identity(_identity):
        return SimpleNamespace(errors=())

    async def record_session_event(uid, session_id, event):
        notifications.append((uid, session_id, event))

    monkeypatch.setattr(terminal_audit_lifecycle_module, "cleanup_terminal_process_identity", cleanup_process_identity)
    monkeypatch.setattr(confirmation_events_module, "send_session_event", record_session_event)
    _patch_terminal_session_factory(monkeypatch, terminal_audit_database)

    audit_record_id, runtimes = await _seed_audit_execution(terminal_audit_database, tmp_path)
    execution_id, terminal_session = runtimes[0]
    audit_root = tmp_path / "audit-root"
    audit_payload = {
        "audit_record_id": audit_record_id,
        "summary": "historical audit context",
        "tool_call_id": "original-call-0",
    }
    audit_file_path = await write_audit_json(
        uid="user-1",
        audit_record_id=audit_record_id,
        payload=audit_payload,
        audit_root=audit_root,
    )

    source_message_content = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            InternalToolCall(
                id="original-call-0",
                name="execute_shell",
                arguments={"command": "python -i"},
            )
        ],
    ).model_dump_json(exclude_none=True)
    tool_result_content = InternalMessage(
        role=MessageRole.TOOL,
        content=json.dumps(
            {"status": "succeeded", "summary": "historical tool result"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        tool_call_id="original-call-0",
        tool_execution_status="succeeded",
    ).model_dump_json(exclude_none=True)
    confirmation_content = json.dumps(
        {
            "audit_record_id": audit_record_id,
            "status": AuditRecordStatus.EXECUTING.value,
            "summary": "historical audit summary",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    async with terminal_audit_database() as db:
        db.add(ChatSession(session_id="chat-session-1", uid="user-1", profile_id=1))
        source_message = Message(
            id=1,
            session_id="chat-session-1",
            uid="user-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content=source_message_content,
            is_processed=True,
        )
        tool_result_message = Message(
            session_id="chat-session-1",
            uid="user-1",
            profile_id=1,
            role=MessageRole.TOOL,
            type=MessageType.TOOL_RESULT,
            content=tool_result_content,
            audit_record_id=audit_record_id,
            audit_tool_call_id="original-call-0",
            is_processed=True,
        )
        confirmation_message = Message(
            session_id="chat-session-1",
            uid="user-1",
            profile_id=1,
            role=MessageRole.ASSISTANT,
            type=MessageType.AUDIT_CONFIRMATION,
            content=confirmation_content,
            is_processed=True,
        )
        db.add_all([source_message, tool_result_message, confirmation_message])
        await db.flush()
        assert source_message.id is not None
        assert tool_result_message.id is not None
        assert confirmation_message.id is not None
        source_message_id = source_message.id
        tool_result_message_id = tool_result_message.id
        confirmation_message_id = confirmation_message.id
        db.add(
            AuditToolResultVersion(
                uid="user-1",
                session_id="chat-session-1",
                audit_record_id=audit_record_id,
                source_assistant_message_id=source_message_id,
                original_tool_call_id="original-call-0",
                message_id=tool_result_message_id,
                version_no=0,
                content=tool_result_message.content or "",
            )
        )
        db.add(
            TerminalControlCommand(
                terminal_session_id=terminal_session.terminal_session_id,
                request_id="stop-audit-history-request",
                action="close",
                payload={"force": True},
                payload_hash="d" * 64,
                status=TerminalControlCommandStatus.PENDING,
            )
        )
        audit_record = await db.get(AuditRecord, audit_record_id)
        assert audit_record is not None
        audit_record.context_file_path = str(audit_file_path)
        await db.commit()

    if terminal_lifecycle == "exited":
        await _TerminalSessionRuntime(terminal_session, WORKER_ID)._update_runtime_snapshot(
            TerminalSessionStatus.EXITED,
            output_buffer=_output_buffer(),
            exit_code=0,
        )

    async def read_audit_state():
        async with terminal_audit_database() as db:
            stored_terminal = await db.get(TerminalSession, terminal_session.terminal_session_id)
            stored_execution = await db.get(AuditExecutionRecord, execution_id)
            stored_record = await db.get(AuditRecord, audit_record_id)
            assert stored_execution is not None
            assert stored_record is not None
            stored_detail = await db.get(AuditToolDetail, stored_execution.audit_tool_detail_id)
            stored_source_message = await db.get(Message, source_message_id)
            stored_tool_result = await db.get(Message, tool_result_message_id)
            stored_confirmation = await db.get(Message, confirmation_message_id)
            version_result = await db.execute(
                select(AuditToolResultVersion).where(
                    AuditToolResultVersion.audit_record_id == audit_record_id,
                    AuditToolResultVersion.original_tool_call_id == "original-call-0",
                )
            )
            stored_version = version_result.scalar_one()
            command_result = await db.execute(
                select(TerminalControlCommand).where(
                    TerminalControlCommand.terminal_session_id == terminal_session.terminal_session_id,
                )
            )
            stored_command = command_result.scalars().first()
            return {
                "terminal": stored_terminal.model_dump() if stored_terminal is not None else None,
                "command": stored_command.model_dump() if stored_command is not None else None,
                "record": stored_record.model_dump(),
                "execution": stored_execution.model_dump(),
                "detail": stored_detail.model_dump() if stored_detail is not None else None,
                "version": stored_version.model_dump(),
                "source_message": dict(stored_source_message) if stored_source_message is not None else None,
                "tool_result": dict(stored_tool_result) if stored_tool_result is not None else None,
                "confirmation": dict(stored_confirmation) if stored_confirmation is not None else None,
            }

    before = await read_audit_state()
    audit_file_before = await asyncio.to_thread(audit_file_path.read_text, encoding="utf-8")
    assert json.loads(audit_file_before) == audit_payload
    assert before["terminal"] is not None
    assert before["command"] is not None
    assert before["record"]["context_file_path"] == str(audit_file_path)
    assert before["detail"] is not None
    assert before["version"] is not None
    assert before["tool_result"] is not None
    assert before["confirmation"] is not None

    async with terminal_audit_database() as db:
        assert await stop_interactive_reply(db, uid="user-1", session_id="chat-session-1", locale="en") == 0
    after = await read_audit_state()
    audit_file_after = await asyncio.to_thread(audit_file_path.read_text, encoding="utf-8")

    async with terminal_audit_database() as db:
        assert await stop_interactive_reply(db, uid="user-1", session_id="chat-session-1", locale="en") == 0
    repeated = await read_audit_state()
    audit_file_repeated = await asyncio.to_thread(audit_file_path.read_text, encoding="utf-8")

    expected_execution_status = AuditExecutionStatus.EXECUTION_UNKNOWN if terminal_lifecycle == "running" else AuditExecutionStatus.SUCCEEDED
    assert after["terminal"] is None
    assert after["command"] is None
    assert repeated["terminal"] is None
    assert repeated["command"] is None
    assert after["execution"]["status"] == expected_execution_status
    assert after["detail"] is not None
    assert after["version"] is not None
    assert after["tool_result"] is not None
    assert after["confirmation"] is not None
    assert after["record"]["status"] == (AuditRecordStatus.EXECUTION_UNKNOWN if terminal_lifecycle == "running" else AuditRecordStatus.SUCCEEDED)
    assert json.loads(after["confirmation"]["content"])["audit_record_id"] == audit_record_id
    assert json.loads(after["confirmation"]["content"])["status"] == after["record"]["status"].value
    assert json.loads(after["confirmation"]["content"])["summary"] == "historical audit summary"
    assert after["record"]["context_file_path"] == before["record"]["context_file_path"] == str(audit_file_path)
    assert after["detail"] == before["detail"]
    assert after["version"] == before["version"]
    assert after["source_message"] == before["source_message"]
    assert after["tool_result"] == before["tool_result"]
    if terminal_lifecycle == "running":
        assert after["execution"]["result_summary"] is not None
        assert after["execution"]["error"] == after["execution"]["result_summary"]
    else:
        assert after["execution"]["result_summary"] == before["execution"]["result_summary"]
        assert after["execution"]["finished_at"] == before["execution"]["finished_at"]
        assert after["record"]["updated_at"] == before["record"]["updated_at"]
        assert after["record"]["completed_at"] == before["record"]["completed_at"]
    assert repeated == after
    assert audit_file_after == audit_file_before == audit_file_repeated
    assert json.loads(audit_file_after) == audit_payload

    status_events = [event for uid, session_id, event in notifications if uid == "user-1" and session_id == "chat-session-1" and event.get("type") == "audit_confirmation_status" and event.get("audit_record_id") == audit_record_id]
    assert status_events
    assert status_events[-1]["status"] == after["record"]["status"].value
