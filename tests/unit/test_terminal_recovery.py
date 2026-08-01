import os

import pytest
from sqlalchemy import delete, update

from app.core.constants import ERR_TERMINAL_SESSION_LEASE_LOST
from app.core.crud.terminal_session import terminal_control_command_crud, terminal_session_crud
from app.core.i18n import t
from app.core.terminal import ALL_TERMINAL_ACTIONS, TerminalSessionStatus, TerminalWriteRequest, manager, recovery
from app.core.terminal.manager import TerminalWorkerCoordinator, terminal_session_manager
from app.core.terminal.recovery import (
    TerminalProcessCleanupResult,
    capture_terminal_process_identity,
    cleanup_terminal_process_identity,
)
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


@pytest.mark.asyncio
async def test_terminal_worker_recovers_stale_unaudited_session_and_pending_command(monkeypatch):
    async def cleanup_without_processes(identity):
        return TerminalProcessCleanupResult((), (), ())

    monkeypatch.setattr(manager, "cleanup_terminal_process_identity", cleanup_without_processes)
    coordinator = TerminalWorkerCoordinator()

    async with AsyncSessionLocal() as db:
        terminal_session = await terminal_session_manager.create_session(
            db,
            uid="recovery-user",
            session_id="recovery-session",
            profile_id=1,
            original_tool_call_id="recovery-tool-call",
            audit_record_id=None,
            audit_execution_record_id=None,
            command="python -i",
            working_directory="temp/recovery-user",
            allowed_actions=ALL_TERMINAL_ACTIONS,
            terminal_session_id="r" * 32,
        )
        database_now = await get_database_timestamp(db)
        await db.execute(
            update(TerminalSession)
            .where(TerminalSession.terminal_session_id == terminal_session.terminal_session_id)
            .values(
                status=TerminalSessionStatus.RUNNING,
                locked_by="stale-worker",
                lock_until=database_now - 1,
                process_identity={"root_pid": 999999},
            )
        )
        await db.commit()

        command, created = await terminal_session_manager.enqueue_control(
            db,
            "recovery-user",
            "recovery-session",
            TerminalWriteRequest(
                terminal_session_id=terminal_session.terminal_session_id,
                request_id="recovery-write-request",
                data="pending",
            ),
        )
        claimed = await terminal_session_crud.claim_next_recoverable(
            db,
            coordinator.worker_id,
            60,
        )

    assert created is True
    assert command.status is TerminalControlCommandStatus.PENDING
    assert claimed is not None
    assert claimed.terminal_session_id == terminal_session.terminal_session_id
    assert claimed.locked_by == coordinator.worker_id

    await coordinator._recover_session(claimed)

    async with AsyncSessionLocal() as db:
        recovered_session = await terminal_session_crud.get(db, terminal_session.terminal_session_id)
        recovered_command = await terminal_control_command_crud.get(db, command.id)
        second_claim = await terminal_session_crud.claim_next_recoverable(db, coordinator.worker_id, 60)

    assert recovered_session is not None
    assert recovered_session.status is TerminalSessionStatus.LOST
    assert recovered_session.failure_reason == t(ERR_TERMINAL_SESSION_LEASE_LOST)
    assert recovered_session.locked_by is None
    assert recovered_session.lock_until is None
    assert recovered_command is not None
    assert recovered_command.status is TerminalControlCommandStatus.FAILED
    assert recovered_command.error == t(ERR_TERMINAL_SESSION_LEASE_LOST)
    assert recovered_command.locked_by is None
    assert recovered_command.lock_until is None
    assert second_claim is None

    await coordinator._recover_session(claimed)

    async with AsyncSessionLocal() as db:
        repeated_session = await terminal_session_crud.get(db, terminal_session.terminal_session_id)
        repeated_command = await terminal_control_command_crud.get(db, command.id)

    assert repeated_session is not None
    assert repeated_session.status is TerminalSessionStatus.LOST
    assert repeated_session.failure_reason == recovered_session.failure_reason
    assert repeated_session.locked_by is None
    assert repeated_session.lock_until is None
    assert repeated_command is not None
    assert repeated_command.status is TerminalControlCommandStatus.FAILED
    assert repeated_command.error == recovered_command.error


@pytest.mark.asyncio
async def test_cleanup_terminal_process_identity_refuses_current_python_process():
    current_pid = os.getpid()
    identity = await capture_terminal_process_identity(current_pid)
    assert identity is not None

    result = await cleanup_terminal_process_identity(identity)

    assert current_pid not in result.terminated_processes
    assert any("refusing to terminate current process" in error for error in result.errors)


@pytest.mark.asyncio
async def test_capture_terminal_process_identity_rejects_reused_root_pid(monkeypatch):
    root_pid = 4242
    old_root_create_time = 10.0
    new_root_create_time = 20.0
    old_child_pid = 4343
    previous_identity = {
        "platform": "Windows",
        "boot_time": 100.0,
        "root_pid": root_pid,
        "root_create_time": old_root_create_time,
        "process_group_id": None,
        "known_processes": {
            str(root_pid): old_root_create_time,
            str(old_child_pid): 30.0,
        },
    }

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            if self.pid == root_pid:
                return new_root_create_time
            return 30.0

    monkeypatch.setattr(recovery.platform, "system", lambda: "Windows")
    monkeypatch.setattr(recovery.psutil, "boot_time", lambda: 100.0)
    monkeypatch.setattr(recovery.psutil, "Process", FakeProcess)

    captured_identity = await capture_terminal_process_identity(root_pid, previous_identity)

    assert captured_identity is not None
    assert captured_identity["root_pid"] == root_pid
    assert captured_identity["root_create_time"] == old_root_create_time
    assert str(root_pid) not in captured_identity["known_processes"]
    assert captured_identity["known_processes"] == {str(old_child_pid): 30.0}
