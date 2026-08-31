import asyncio
import os
import subprocess
import sys

import psutil
import pytest
from sqlalchemy import delete, update

from app.core.constants import ERR_TERMINAL_SESSION_LEASE_LOST
from app.core.crud.terminal.session import terminal_control_command_crud, terminal_session_crud
from app.core.i18n import t
from app.core.terminal import ALL_TERMINAL_ACTIONS, TerminalSessionStatus, TerminalWriteRequest, manager, recovery
from app.core.terminal.manager import TerminalWorkerCoordinator, terminal_session_manager
from app.core.terminal.recovery import (
    TerminalProcessCleanupResult,
    capture_terminal_process_identity,
    cleanup_terminal_process_identity,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.terminal_session import TerminalControlCommand, TerminalControlCommandStatus, TerminalSession
from app.providers.database import AsyncSessionLocal, engine
from app.providers.database.time import get_database_timestamp


def _process_has_exited(pid: int, create_time: float) -> bool:
    try:
        process = psutil.Process(pid)
        if process.create_time() != create_time:
            return True
        return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True


async def _wait_for_process_exit(pid: int, create_time: float, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await asyncio.to_thread(_process_has_exited, pid, create_time):
            return True
        await asyncio.sleep(0.05)
    return False


def _kill_test_process_tree(root_process: subprocess.Popen[str], child_pid: int | None) -> None:
    child_processes: list[psutil.Process] = []
    try:
        child_processes.extend(psutil.Process(root_process.pid).children(recursive=True))
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        pass

    if child_pid is not None and child_pid != os.getpid():
        try:
            child_process = psutil.Process(child_pid)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
        else:
            if all(process.pid != child_pid for process in child_processes):
                child_processes.append(child_process)

    for process in child_processes:
        if process.pid == os.getpid():
            continue
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass

    for process in child_processes:
        try:
            process.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass

    if root_process.poll() is None:
        root_process.kill()
    try:
        root_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        root_process.kill()
        root_process.wait(timeout=5)


@pytest.fixture(autouse=True)
async def isolated_terminal_database():
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: TerminalControlCommand.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: TerminalSession.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: PromptLibrary.__table__.create(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: Profile.__table__.create(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: ChatSession.__table__.create(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: TerminalSession.__table__.create(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: TerminalControlCommand.__table__.create(sync_connection, checkfirst=True))

    async with AsyncSessionLocal() as db:
        await db.execute(delete(ChatSession).where(ChatSession.session_id == "recovery-session"))
        db.add(ChatSession(session_id="recovery-session", uid="recovery-user"))
        await db.commit()

    try:
        yield
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(TerminalControlCommand))
            await db.execute(delete(TerminalSession))
            await db.execute(delete(ChatSession).where(ChatSession.session_id == "recovery-session"))
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


@pytest.mark.asyncio
async def test_cleanup_terminal_process_identity_terminates_root_and_child():
    root_code = "import subprocess, sys, time\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\nprint(child.pid, flush=True)\ntime.sleep(60)\n"
    if os.name == "nt":
        kwargs = {
            "stdout": subprocess.PIPE,
            "text": True,
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    else:
        kwargs = {"stdout": subprocess.PIPE, "text": True, "start_new_session": True}
    root_process = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-c", root_code],
        **kwargs,
    )

    child_pid: int | None = None
    try:
        assert root_process.stdout is not None
        child_output = await asyncio.to_thread(root_process.stdout.readline)
        child_pid = int(child_output.strip())
        identity = await capture_terminal_process_identity(root_process.pid)

        assert identity is not None
        assert str(root_process.pid) in identity["known_processes"]
        assert str(child_pid) in identity["known_processes"]

        result = await cleanup_terminal_process_identity(identity)

        assert result.errors == ()
        assert await _wait_for_process_exit(
            root_process.pid,
            identity["known_processes"][str(root_process.pid)],
        )
        assert await _wait_for_process_exit(
            child_pid,
            identity["known_processes"][str(child_pid)],
        )
    finally:
        await asyncio.to_thread(_kill_test_process_tree, root_process, child_pid)


@pytest.mark.asyncio
async def test_cleanup_terminal_process_identity_rejects_reused_root_pid(monkeypatch):
    root_pid = os.getpid() + 100_000
    root_create_time = 10.0
    kill_calls: list[str] = []

    class ReusedProcess:
        def __init__(self, pid):
            assert pid == root_pid
            self.pid = pid

        def create_time(self):
            return 20.0

        def terminate(self):
            kill_calls.append("terminate")

        def kill(self):
            kill_calls.append("kill")

    platform_name = recovery.platform.system()
    identity = {
        "platform": platform_name,
        "boot_time": recovery.psutil.boot_time(),
        "root_pid": root_pid,
        "root_create_time": root_create_time,
        "process_group_id": root_pid if platform_name != "Windows" else None,
        "known_processes": {str(root_pid): root_create_time},
    }
    monkeypatch.setattr(recovery.psutil, "Process", ReusedProcess)

    if platform_name != "Windows":
        monkeypatch.setattr(
            recovery.os,
            "killpg",
            lambda process_group_id, signal_number: kill_calls.append("killpg"),
        )

    result = await cleanup_terminal_process_identity(identity)

    assert root_pid not in result.terminated_processes
    assert kill_calls == []


@pytest.mark.asyncio
async def test_cleanup_terminal_process_identity_rejects_different_boot_time(monkeypatch):
    root_pid = os.getpid() + 100_000
    root_create_time = 10.0
    platform_name = recovery.platform.system()
    process_queries: list[int] = []
    identity = {
        "platform": platform_name,
        "boot_time": recovery.psutil.boot_time() - 1.0,
        "root_pid": root_pid,
        "root_create_time": root_create_time,
        "process_group_id": root_pid if platform_name != "Windows" else None,
        "known_processes": {str(root_pid): root_create_time},
    }

    def fail_process_query(pid):
        process_queries.append(pid)
        raise AssertionError(f"unexpected process query for PID {pid}")

    monkeypatch.setattr(recovery.psutil, "Process", fail_process_query)

    result = await cleanup_terminal_process_identity(identity)

    assert result.terminated_processes == ()
    assert result.errors
    assert process_queries == []
