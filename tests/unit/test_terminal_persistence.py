import pytest
from pydantic import ValidationError
from sqlalchemy import delete, update

from app.core.constants import (
    ERR_TERMINAL_ACTION_NOT_ALLOWED,
    ERR_TERMINAL_COMMAND_REQUEST_CONFLICT,
    ERR_TERMINAL_SESSION_ACCESS_DENIED,
    ERR_TERMINAL_SESSION_NOT_FOUND,
)
from app.core.crud.terminal_session import terminal_control_command_crud, terminal_session_crud
from app.core.exceptions import ForbiddenException, ParameterException, ResourceNotFoundException
from app.core.terminal import (
    ALL_TERMINAL_ACTIONS,
    TerminalAction,
    TerminalResizeRequest,
    TerminalSessionStatus,
    TerminalWriteRequest,
)
from app.core.terminal.manager import terminal_session_manager
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession
from app.providers.database import AsyncSessionLocal, engine
from app.providers.database.time import get_database_timestamp


@pytest.fixture(autouse=True)
async def isolated_terminal_database():
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: TerminalControlCommand.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: TerminalSession.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: TerminalSession.__table__.create(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: TerminalControlCommand.__table__.create(sync_connection, checkfirst=True))

    try:
        yield
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(TerminalControlCommand))
            await db.execute(delete(TerminalSession))
            await db.commit()


async def create_terminal_session(
    *,
    uid: str = "user-1",
    session_id: str = "chat-session-1",
    profile_id: int = 1,
    original_tool_call_id: str = "tool-call-1",
    audit_record_id: int = 1,
    audit_execution_record_id: int = 1,
    command: str = "python -i",
    working_directory: str = "temp/user-1",
    allowed_actions: set[TerminalAction] | None = None,
    output_capacity_bytes: int = 1_048_576,
    terminal_session_id: str | None = None,
) -> TerminalSession:
    actions = ALL_TERMINAL_ACTIONS if allowed_actions is None else allowed_actions
    async with AsyncSessionLocal() as db:
        return await terminal_session_manager.create_session(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            original_tool_call_id=original_tool_call_id,
            audit_record_id=audit_record_id,
            audit_execution_record_id=audit_execution_record_id,
            command=command,
            working_directory=working_directory,
            allowed_actions=actions,
            output_capacity_bytes=output_capacity_bytes,
            terminal_session_id=terminal_session_id,
        )


@pytest.mark.asyncio
async def test_terminal_session_snapshot_persists_across_database_sessions():
    terminal_session = await create_terminal_session(
        terminal_session_id="p" * 32,
        audit_record_id=101,
        audit_execution_record_id=1001,
    )

    async with AsyncSessionLocal() as db:
        snapshot = await terminal_session_manager.get_snapshot(
            db,
            terminal_session.terminal_session_id,
            "user-1",
            "chat-session-1",
        )

    assert snapshot.terminal_session_id == "p" * 32
    assert snapshot.status is TerminalSessionStatus.STARTING
    assert snapshot.permission_scope.owner_uid == "user-1"
    assert snapshot.permission_scope.owner_session_id == "chat-session-1"
    assert snapshot.permission_scope.original_tool_call_id == "tool-call-1"
    assert snapshot.permission_scope.audit_record_id == 101
    assert snapshot.permission_scope.audit_execution_record_id == 1001
    assert snapshot.permission_scope.allowed_actions == ALL_TERMINAL_ACTIONS
    assert snapshot.output_buffer.capacity_bytes == 1_048_576
    assert snapshot.output_buffer.oldest_offset == 0
    assert snapshot.output_buffer.next_offset == 0
    assert snapshot.output_buffer.oldest_sequence == 1
    assert snapshot.output_buffer.next_sequence == 1


@pytest.mark.asyncio
async def test_terminal_session_snapshot_accepts_legacy_signal_action():
    terminal_session = await create_terminal_session(
        terminal_session_id="s" * 32,
        audit_record_id=110,
        audit_execution_record_id=1010,
    )

    async with AsyncSessionLocal() as db:
        await db.execute(update(TerminalSession).where(TerminalSession.terminal_session_id == terminal_session.terminal_session_id).values(allowed_actions=["status", "read", "write", "resize", "signal", "close"]))
        await db.commit()

    async with AsyncSessionLocal() as db:
        snapshot = await terminal_session_manager.get_snapshot(
            db,
            terminal_session.terminal_session_id,
            "user-1",
            "chat-session-1",
        )

    assert snapshot.permission_scope.allowed_actions == ALL_TERMINAL_ACTIONS


@pytest.mark.asyncio
async def test_terminal_session_snapshot_reports_not_found_and_access_denied():
    terminal_session = await create_terminal_session(
        terminal_session_id="q" * 32,
        audit_record_id=102,
        audit_execution_record_id=1002,
    )

    async with AsyncSessionLocal() as db:
        with pytest.raises(ResourceNotFoundException) as not_found:
            await terminal_session_manager.get_snapshot(db, "missing-terminal-session", "user-1", "chat-session-1")
        with pytest.raises(ForbiddenException) as wrong_uid:
            await terminal_session_manager.get_snapshot(db, terminal_session.terminal_session_id, "user-2", "chat-session-1")
        with pytest.raises(ForbiddenException) as wrong_session:
            await terminal_session_manager.get_snapshot(db, terminal_session.terminal_session_id, "user-1", "chat-session-2")

    assert not_found.value.message == ERR_TERMINAL_SESSION_NOT_FOUND
    assert wrong_uid.value.message == ERR_TERMINAL_SESSION_ACCESS_DENIED
    assert wrong_session.value.message == ERR_TERMINAL_SESSION_ACCESS_DENIED


@pytest.mark.asyncio
async def test_terminal_session_create_validates_custom_id_and_output_capacity():
    with pytest.raises(ValidationError):
        await create_terminal_session(
            terminal_session_id="short-terminal-id",
            audit_record_id=107,
            audit_execution_record_id=1007,
        )

    with pytest.raises(ValidationError):
        await create_terminal_session(
            output_capacity_bytes=0,
            audit_record_id=108,
            audit_execution_record_id=1008,
        )


@pytest.mark.asyncio
async def test_terminal_control_enqueue_is_idempotent_and_permission_scoped():
    terminal_session = await create_terminal_session(
        terminal_session_id="w" * 32,
        audit_record_id=103,
        audit_execution_record_id=1003,
        allowed_actions={TerminalAction.WRITE},
    )
    request = TerminalWriteRequest(
        terminal_session_id=terminal_session.terminal_session_id,
        request_id="write-request-0001",
        data="echo hello",
    )

    async with AsyncSessionLocal() as first_db:
        first_command, first_created = await terminal_session_manager.enqueue_control(
            first_db,
            "user-1",
            "chat-session-1",
            request,
        )

    async with AsyncSessionLocal() as second_db:
        second_command, second_created = await terminal_session_manager.enqueue_control(
            second_db,
            "user-1",
            "chat-session-1",
            request,
        )
        with pytest.raises(ParameterException) as conflict:
            await terminal_session_manager.enqueue_control(
                second_db,
                "user-1",
                "chat-session-1",
                TerminalWriteRequest(
                    terminal_session_id=terminal_session.terminal_session_id,
                    request_id=request.request_id,
                    data="echo different",
                ),
            )
        with pytest.raises(ForbiddenException) as forbidden:
            await terminal_session_manager.enqueue_control(
                second_db,
                "user-1",
                "chat-session-1",
                TerminalResizeRequest(
                    terminal_session_id=terminal_session.terminal_session_id,
                    request_id="resize-request-0001",
                    columns=80,
                    rows=24,
                ),
            )

    assert first_created is True
    assert second_created is False
    assert second_command.id == first_command.id
    assert second_command.payload == {"action": "write", "data": "echo hello"}
    assert second_command.action is TerminalAction.WRITE
    assert conflict.value.message == ERR_TERMINAL_COMMAND_REQUEST_CONFLICT
    assert forbidden.value.message == ERR_TERMINAL_ACTION_NOT_ALLOWED


@pytest.mark.asyncio
async def test_terminal_starting_claims_are_ordered_exclusive_and_recoverable():
    first_session = await create_terminal_session(
        terminal_session_id="a" * 32,
        audit_record_id=104,
        audit_execution_record_id=1004,
    )
    second_session = await create_terminal_session(
        terminal_session_id="b" * 32,
        audit_record_id=105,
        audit_execution_record_id=1005,
    )

    async with AsyncSessionLocal() as db:
        first_claim = await terminal_session_crud.claim_next_starting(db, "worker-a", 60)
        second_claim = await terminal_session_crud.claim_next_starting(db, "worker-a", 60)
        third_claim = await terminal_session_crud.claim_next_starting(db, "worker-a", 60)
        assert first_claim is not None
        assert second_claim is not None
        blocked_claim = await terminal_session_crud.try_claim_starting(db, first_claim.terminal_session_id, "worker-b", 60)

        database_now = await get_database_timestamp(db)
        await db.execute(update(TerminalSession).where(TerminalSession.terminal_session_id == first_claim.terminal_session_id).values(lock_until=database_now - 1))
        await db.commit()

        takeover = await terminal_session_crud.try_claim_starting(db, first_claim.terminal_session_id, "worker-b", 60)
        old_worker_renewed = await terminal_session_crud.renew_lease(db, first_claim.terminal_session_id, "worker-a", 60)
        new_worker_renewed = await terminal_session_crud.renew_lease(db, first_claim.terminal_session_id, "worker-b", 60)
        released = await terminal_session_crud.release_starting_claim(db, first_claim.terminal_session_id, "worker-b")

    async with AsyncSessionLocal() as db:
        released_session = await terminal_session_crud.get(db, first_claim.terminal_session_id)

    assert first_claim.terminal_session_id != second_claim.terminal_session_id
    assert {first_claim.terminal_session_id, second_claim.terminal_session_id} == {
        first_session.terminal_session_id,
        second_session.terminal_session_id,
    }
    assert third_claim is None
    assert blocked_claim is None
    assert takeover is not None
    assert takeover.terminal_session_id == first_claim.terminal_session_id
    assert takeover.locked_by == "worker-b"
    assert old_worker_renewed is False
    assert new_worker_renewed is True
    assert released is True
    assert released_session is not None
    assert released_session.locked_by is None
    assert released_session.lock_until is None


@pytest.mark.asyncio
async def test_list_active_audit_execution_record_ids_excludes_final_sessions_and_other_audits():
    cases = [
        (TerminalSessionStatus.STARTING, 200, 2001),
        (TerminalSessionStatus.RUNNING, 200, 2002),
        (TerminalSessionStatus.CLOSING, 200, 2003),
        (TerminalSessionStatus.EXITED, 200, 2004),
        (TerminalSessionStatus.FAILED, 200, 2005),
        (TerminalSessionStatus.LOST, 200, 2006),
        (TerminalSessionStatus.STARTING, 201, 2007),
    ]
    for index, (status, audit_record_id, audit_execution_record_id) in enumerate(cases):
        await create_terminal_session(
            terminal_session_id=f"l{index}" + "s" * 31,
            audit_record_id=audit_record_id,
            audit_execution_record_id=audit_execution_record_id,
        )
        async with AsyncSessionLocal() as db:
            await db.execute(update(TerminalSession).where(TerminalSession.terminal_session_id == f"l{index}" + "s" * 31).values(status=status))
            await db.commit()

    async with AsyncSessionLocal() as db:
        active_ids = await terminal_session_crud.list_active_audit_execution_record_ids(db, audit_record_id=200)

    assert active_ids == {2001, 2002, 2003}


@pytest.mark.asyncio
async def test_terminal_command_claim_requires_session_lease_and_owner_completion():
    terminal_session = await create_terminal_session(
        terminal_session_id="c" * 32,
        audit_record_id=106,
        audit_execution_record_id=1006,
        allowed_actions={TerminalAction.WRITE},
    )
    request = TerminalWriteRequest(
        terminal_session_id=terminal_session.terminal_session_id,
        request_id="write-request-0002",
        data="print('ready')",
    )

    async with AsyncSessionLocal() as db:
        command, created = await terminal_session_manager.enqueue_control(
            db,
            "user-1",
            "chat-session-1",
            request,
        )
        assert created is True
        assert await terminal_control_command_crud.claim_next(db, terminal_session.terminal_session_id, "worker-b", 60) is None
        session_claim = await terminal_session_crud.try_claim_starting(db, terminal_session.terminal_session_id, "worker-a", 60)
        blocked_command = await terminal_control_command_crud.claim_next(db, terminal_session.terminal_session_id, "worker-b", 60)
        claimed_command = await terminal_control_command_crud.claim_next(db, terminal_session.terminal_session_id, "worker-a", 60)
        duplicate_claim = await terminal_control_command_crud.claim_next(db, terminal_session.terminal_session_id, "worker-a", 60)
        wrong_worker_marked = await terminal_control_command_crud.mark_succeeded(
            db,
            command.id,
            "worker-b",
            {"stdout": "wrong"},
        )
        correct_worker_marked = await terminal_control_command_crud.mark_succeeded(
            db,
            command.id,
            "worker-a",
            {"stdout": "ready"},
        )

    async with AsyncSessionLocal() as db:
        commands = await terminal_control_command_crud.list_by_session(db, terminal_session.terminal_session_id)

    assert session_claim is not None
    assert blocked_command is None
    assert claimed_command is not None
    assert claimed_command.status is TerminalControlCommandStatus.PROCESSING
    assert claimed_command.locked_by == "worker-a"
    assert duplicate_claim is None
    assert wrong_worker_marked is False
    assert correct_worker_marked is True
    assert len(commands) == 1
    assert commands[0].status is TerminalControlCommandStatus.SUCCEEDED
    assert commands[0].result == {"stdout": "ready"}
    assert commands[0].locked_by is None
    assert commands[0].lock_until is None


@pytest.mark.asyncio
async def test_terminal_command_completion_requires_current_session_lease_owner():
    terminal_session = await create_terminal_session(
        terminal_session_id="d" * 32,
        audit_record_id=109,
        audit_execution_record_id=1009,
        allowed_actions={TerminalAction.WRITE},
    )
    request = TerminalWriteRequest(
        terminal_session_id=terminal_session.terminal_session_id,
        request_id="write-request-0003",
        data="print('ready')",
    )

    async with AsyncSessionLocal() as db:
        command, created = await terminal_session_manager.enqueue_control(
            db,
            "user-1",
            "chat-session-1",
            request,
        )
        session_claim = await terminal_session_crud.try_claim_starting(
            db,
            terminal_session.terminal_session_id,
            "worker-a",
            60,
        )
        claimed_command = await terminal_control_command_crud.claim_next(
            db,
            terminal_session.terminal_session_id,
            "worker-a",
            60,
        )
        database_now = await get_database_timestamp(db)
        await db.execute(update(TerminalSession).where(TerminalSession.terminal_session_id == terminal_session.terminal_session_id).values(lock_until=database_now - 1))
        await db.commit()

        takeover = await terminal_session_crud.try_claim_starting(
            db,
            terminal_session.terminal_session_id,
            "worker-b",
            60,
        )
        old_worker_marked = await terminal_control_command_crud.mark_succeeded(
            db,
            command.id,
            "worker-a",
            {"stdout": "stale"},
        )
        command_after = await terminal_control_command_crud.get(db, command.id)
        command_lease_now = await get_database_timestamp(db)

    assert created is True
    assert session_claim is not None
    assert claimed_command is not None
    assert takeover is not None
    assert takeover.locked_by == "worker-b"
    assert old_worker_marked is False
    assert command_after is not None
    assert command_after.status is TerminalControlCommandStatus.PROCESSING
    assert command_after.locked_by == "worker-a"
    assert command_after.lock_until >= command_lease_now


def test_terminal_model_metadata_marks_persisted_fields_not_nullable():
    for column_name in ("command", "working_directory", "allowed_actions", "created_at", "updated_at"):
        assert TerminalSession.__table__.c[column_name].nullable is False
    for column_name in ("payload", "created_at", "updated_at"):
        assert TerminalControlCommand.__table__.c[column_name].nullable is False
