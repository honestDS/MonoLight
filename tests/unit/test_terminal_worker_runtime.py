import asyncio
import os
import shlex
import subprocess
import sys
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import delete

from app.core.terminal import (
    ALL_TERMINAL_ACTIONS,
    TerminalCloseRequest,
    TerminalReadRequest,
    TerminalReadResult,
    TerminalResizeRequest,
    TerminalSessionStatus,
    TerminalWriteRequest,
)
from app.core.terminal.manager import TerminalWorkerCoordinator, terminal_session_manager
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


async def _enqueue_read_result(terminal_session_id: str, request_id: str) -> TerminalReadResult:
    read_request = TerminalReadRequest(
        terminal_session_id=terminal_session_id,
        offset=0,
        max_bytes=65_536,
    )
    async with AsyncSessionLocal() as db:
        read_command, read_created = await terminal_session_manager.enqueue_read(
            db,
            "terminal-runtime-user",
            "terminal-runtime-session",
            read_request,
            request_id,
        )
    assert read_created is True
    return TerminalReadResult.model_validate(await _wait_for_command(read_command.id))


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

        newline = "\r\n" if sys.platform == "win32" else "\n"
        write_request = TerminalWriteRequest(
            terminal_session_id=terminal_session.terminal_session_id,
            request_id="w" * 16,
            data='import os; print("stage5-ready"); print("CWD:" + os.getcwd())' + newline,
        )
        async with AsyncSessionLocal() as db:
            write_command, write_created = await terminal_session_manager.enqueue_control(
                db,
                "terminal-runtime-user",
                "terminal-runtime-session",
                write_request,
            )
        assert write_created is True
        write_result = await _wait_for_command(write_command.id)
        assert write_result["bytes_written"] > 0

        await _wait_until(
            lambda: _get_snapshot(terminal_session.terminal_session_id),
            lambda snapshot: snapshot.output_buffer.next_offset > running_snapshot.output_buffer.next_offset,
        )

        read_result = None
        read_deadline = asyncio.get_running_loop().time() + STEP_TIMEOUT
        read_attempt = 0
        while read_result is None or not ("stage5-ready" in read_result.output and "CWD:" in read_result.output and os.getcwd() in read_result.output):
            read_result = await _enqueue_read_result(
                terminal_session.terminal_session_id,
                "r" * 16 + f"-{read_attempt}",
            )
            if "stage5-ready" in read_result.output and "CWD:" in read_result.output and os.getcwd() in read_result.output:
                break
            remaining = read_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(f"timed out waiting for terminal output: {read_result!r}")
            read_attempt += 1
            await asyncio.sleep(min(0.05, remaining))

        assert "stage5-ready" in read_result.output
        assert "CWD:" in read_result.output
        assert os.getcwd() in read_result.output
        assert read_result.requested_offset == 0
        assert 0 <= read_result.start_offset <= read_result.next_offset <= read_result.latest_offset
        assert read_result.sequence >= 1
        assert read_result.eof is False

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
