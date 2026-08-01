import asyncio
import json
import os
import platform
import shlex
import subprocess
import sys
from collections.abc import Awaitable, Callable

import psutil
import pytest
from sqlalchemy import delete

from app.core.constants import ERR_TERMINAL_WORKER_STOPPED
from app.core.dispatch_context import DispatchContext
from app.core.i18n import t
from app.core.terminal import (
    ALL_TERMINAL_ACTIONS,
    TerminalCloseRequest,
    TerminalReadRequest,
    TerminalReadResult,
    TerminalResizeRequest,
    TerminalSessionStatus,
    TerminalWriteResult,
)
from app.core.terminal.manager import TerminalWorkerCoordinator, terminal_session_manager
from app.core.tools.terminal import TerminalWriteExecutor
from app.models.audit import AuditExecutionRecord
from app.models.profile import Profile, ProfileConfig
from app.models.terminal_session import TerminalControlCommand, TerminalSession
from app.providers.database import AsyncSessionLocal, engine

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" and not sys.platform.startswith("linux"),
    reason="Terminal worker PTY runtime requires Windows ConPTY or Linux PTY",
)

STEP_TIMEOUT = 10.0


@pytest.fixture(autouse=True)
async def isolated_terminal_database():
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: AuditExecutionRecord.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: TerminalControlCommand.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: TerminalSession.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: AuditExecutionRecord.__table__.create(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: TerminalSession.__table__.create(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: TerminalControlCommand.__table__.create(sync_connection, checkfirst=True))

    try:
        yield
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(AuditExecutionRecord))
            await db.execute(delete(TerminalControlCommand))
            await db.execute(delete(TerminalSession))
            await db.commit()


def _interactive_python_command() -> str:
    argv = [sys.executable, "-u", "-i"]
    if sys.platform == "win32":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


async def _wait_until(
    read_value: Callable[[], Awaitable[object]],
    predicate: Callable[[object], bool],
    *,
    timeout: float = STEP_TIMEOUT,
) -> object:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await read_value()
        if predicate(value):
            return value
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for terminal state; last value: {value!r}")
        await asyncio.sleep(min(0.05, remaining))


async def _get_snapshot(terminal_session_id: str):
    async with AsyncSessionLocal() as db:
        return await terminal_session_manager.get_snapshot(
            db,
            terminal_session_id,
            "terminal-runtime-user",
            "terminal-runtime-session",
        )


async def _wait_for_command(command_id: int):
    async with AsyncSessionLocal() as db:
        return await asyncio.wait_for(
            terminal_session_manager.wait_for_command_result(db, command_id, STEP_TIMEOUT),
            timeout=STEP_TIMEOUT + 1.0,
        )


@pytest.mark.asyncio
async def test_terminal_worker_runs_real_interactive_python_session():
    command = _interactive_python_command()
    coordinator = TerminalWorkerCoordinator()
    terminal_session = None

    try:
        async with AsyncSessionLocal() as db:
            terminal_session = await terminal_session_manager.create_session(
                db,
                uid="terminal-runtime-user",
                session_id="terminal-runtime-session",
                profile_id=1,
                original_tool_call_id="virtual-tool-call",
                audit_record_id=1,
                audit_execution_record_id=1,
                command=command,
                working_directory=os.getcwd(),
                allowed_actions=ALL_TERMINAL_ACTIONS,
            )

        coordinator.start()
        running_snapshot = await _wait_until(
            lambda: _get_snapshot(terminal_session.terminal_session_id),
            lambda snapshot: snapshot.status is TerminalSessionStatus.RUNNING,
        )
        assert running_snapshot.status is TerminalSessionStatus.RUNNING
        async with AsyncSessionLocal() as db:
            stored_terminal = await db.get(TerminalSession, terminal_session.terminal_session_id)
        assert stored_terminal is not None
        process_identity = stored_terminal.process_identity
        assert process_identity is not None
        assert process_identity["platform"] == platform.system()
        assert isinstance(process_identity["boot_time"], (int, float))
        assert isinstance(process_identity["root_pid"], int)
        assert isinstance(process_identity["root_create_time"], (int, float))
        assert isinstance(process_identity["known_processes"], dict)
        root_process = psutil.Process(process_identity["root_pid"])
        assert root_process.is_running()
        assert root_process.status() != psutil.STATUS_ZOMBIE

        write_executor = TerminalWriteExecutor(project_root=os.getcwd(), uid="terminal-runtime-user")
        write_executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": STEP_TIMEOUT}}))
        async with AsyncSessionLocal() as db:
            write_executor.set_runtime_context(
                dispatch_context=DispatchContext(
                    mode="interactive",
                    source="test",
                    uid="terminal-runtime-user",
                    session_id="terminal-runtime-session",
                    profile=Profile(id=1, uid="terminal-runtime-user", name="terminal-runtime", configs={}),
                    db=db,
                    tool_call_id="terminal-runtime-write-tool-call",
                )
            )
            write_result = TerminalWriteResult.model_validate(
                json.loads(
                    await write_executor.execute(
                        terminal_session_id=terminal_session.terminal_session_id,
                        data='import os; print("stage5-ready"); print("CWD:" + os.getcwd())' + "\n",
                    )
                )
            )
        assert write_result.bytes_written > 0
        assert write_result.read_timed_out is False
        assert write_result.read_result is not None
        assert "stage5-ready" in write_result.read_result.output
        assert "CWD:" in write_result.read_result.output
        assert os.getcwd() in write_result.read_result.output
        assert write_result.read_result.eof is False

        explicit_read_request = TerminalReadRequest(
            terminal_session_id=terminal_session.terminal_session_id,
            offset=0,
            max_bytes=65_536,
        )
        async with AsyncSessionLocal() as db:
            explicit_read_command, explicit_read_created = await terminal_session_manager.enqueue_read(
                db,
                "terminal-runtime-user",
                "terminal-runtime-session",
                explicit_read_request,
                "r" * 16,
            )
        assert explicit_read_created is True
        explicit_read_result = TerminalReadResult.model_validate(await _wait_for_command(explicit_read_command.id))
        assert "stage5-ready" in explicit_read_result.output
        assert "CWD:" in explicit_read_result.output
        assert os.getcwd() in explicit_read_result.output
        assert explicit_read_result.requested_offset == 0
        assert 0 <= explicit_read_result.start_offset <= explicit_read_result.next_offset <= explicit_read_result.latest_offset
        assert explicit_read_result.sequence >= 1
        assert explicit_read_result.eof is False

        resize_request = TerminalResizeRequest(
            terminal_session_id=terminal_session.terminal_session_id,
            request_id="z" * 16,
            columns=100,
            rows=40,
        )
        async with AsyncSessionLocal() as db:
            resize_command, resize_created = await terminal_session_manager.enqueue_control(
                db,
                "terminal-runtime-user",
                "terminal-runtime-session",
                resize_request,
            )
        assert resize_created is True
        assert await _wait_for_command(resize_command.id) == {"columns": 100, "rows": 40}

        close_request = TerminalCloseRequest(
            terminal_session_id=terminal_session.terminal_session_id,
            request_id="c" * 16,
            force=True,
        )
        async with AsyncSessionLocal() as db:
            close_command, close_created = await terminal_session_manager.enqueue_control(
                db,
                "terminal-runtime-user",
                "terminal-runtime-session",
                close_request,
            )
        assert close_created is True
        close_result = await _wait_for_command(close_command.id)
        assert close_result["status"] == "exited"
        exited_snapshot = await _wait_until(
            lambda: _get_snapshot(terminal_session.terminal_session_id),
            lambda snapshot: snapshot.status is TerminalSessionStatus.EXITED,
        )
        assert exited_snapshot.status is TerminalSessionStatus.EXITED

        retained_read_request = TerminalReadRequest(
            terminal_session_id=terminal_session.terminal_session_id,
            offset=0,
            max_bytes=65_536,
        )
        async with AsyncSessionLocal() as db:
            retained_read_command, retained_read_created = await terminal_session_manager.enqueue_read(
                db,
                "terminal-runtime-user",
                "terminal-runtime-session",
                retained_read_request,
                "r" * 16 + "-after-exit",
            )
        assert retained_read_created is True
        retained_read_result = TerminalReadResult.model_validate(await _wait_for_command(retained_read_command.id))
        assert "stage5-ready" in retained_read_result.output
        assert "CWD:" in retained_read_result.output
        assert os.getcwd() in retained_read_result.output
        assert retained_read_result.eof is True

        async with AsyncSessionLocal() as db:
            repeated_close_command, repeated_close_created = await terminal_session_manager.enqueue_control(
                db,
                "terminal-runtime-user",
                "terminal-runtime-session",
                close_request,
            )
        assert repeated_close_created is False
        assert await _wait_for_command(repeated_close_command.id) == close_result
    finally:
        await asyncio.wait_for(coordinator.stop(), timeout=STEP_TIMEOUT)


@pytest.mark.asyncio
async def test_terminal_worker_stop_marks_unaudited_interactive_session_lost_and_kills_process():
    command = _interactive_python_command()
    coordinator = TerminalWorkerCoordinator()
    terminal_session = None
    process_identity = None

    try:
        async with AsyncSessionLocal() as db:
            terminal_session = await terminal_session_manager.create_session(
                db,
                uid="terminal-runtime-user",
                session_id="terminal-runtime-session",
                profile_id=1,
                original_tool_call_id="unaudited-virtual-tool-call",
                audit_record_id=None,
                audit_execution_record_id=None,
                command=command,
                working_directory=os.getcwd(),
                allowed_actions=ALL_TERMINAL_ACTIONS,
            )

        coordinator.start()
        running_snapshot = await _wait_until(
            lambda: _get_snapshot(terminal_session.terminal_session_id),
            lambda snapshot: snapshot.status is TerminalSessionStatus.RUNNING,
        )
        assert running_snapshot.status is TerminalSessionStatus.RUNNING
        async with AsyncSessionLocal() as db:
            stored_terminal = await db.get(TerminalSession, terminal_session.terminal_session_id)
        assert stored_terminal is not None
        process_identity = stored_terminal.process_identity
        assert process_identity is not None

        await coordinator.stop()

        async with AsyncSessionLocal() as db:
            stored_terminal = await db.get(TerminalSession, terminal_session.terminal_session_id)
        assert stored_terminal is not None
        assert stored_terminal.status is TerminalSessionStatus.LOST
        assert stored_terminal.failure_reason == t(ERR_TERMINAL_WORKER_STOPPED)
        assert stored_terminal.locked_by is None
        assert stored_terminal.lock_until is None

        root_pid = process_identity["root_pid"]
        root_create_time = process_identity["root_create_time"]
        try:
            root_process = psutil.Process(root_pid)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
        else:
            assert not root_process.is_running() or root_process.status() == psutil.STATUS_ZOMBIE or root_process.create_time() != root_create_time
    finally:
        await asyncio.wait_for(coordinator.stop(), timeout=STEP_TIMEOUT)
