import asyncio
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.core.dispatch_context import DispatchContext
from app.core.terminal import (
    ShellInteractiveHandoffResult,
    TerminalAction,
    TerminalActionReceipt,
    TerminalReadResult,
    TerminalSessionSnapshot,
    TerminalSessionStatus,
    TerminalWriteResult,
)
from app.core.terminal.manager import TerminalWorkerCoordinator, terminal_session_manager
from app.core.tools.shell import ShellExecutor
from app.core.tools.terminal import (
    TerminalCloseExecutor,
    TerminalReadExecutor,
    TerminalResizeExecutor,
    TerminalStatusExecutor,
    TerminalWriteExecutor,
)
from app.models.profile import Profile, ProfileConfig
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.terminal_session import TerminalControlCommand, TerminalSession
from app.providers.database import AsyncSessionLocal, engine

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" and not sys.platform.startswith("linux"),
    reason="Terminal shell workflow requires Windows ConPTY or Linux PTY",
)

STEP_TIMEOUT = 10.0
TTY_MARKERS = ("TTY_STDIN=True", "TTY_STDOUT=True", "TTY_STDERR=True")
ECHO_MARKER = "TERMINAL_WORKFLOW_ECHO_7D9A"
TEST_UID = "terminal-shell-workflow-user"
TEST_SESSION_ID = "terminal-shell-workflow-session"


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
        await db.execute(delete(ChatSession).where(ChatSession.session_id == TEST_SESSION_ID))
        db.add(ChatSession(session_id=TEST_SESSION_ID, uid=TEST_UID))
        await db.commit()

    try:
        yield
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(TerminalControlCommand))
            await db.execute(delete(TerminalSession))
            await db.execute(delete(ChatSession).where(ChatSession.session_id == TEST_SESSION_ID))
            await db.commit()


def _interactive_python_command(script_path: Path) -> str:
    argv = [sys.executable, "-u", str(script_path)]
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


async def _get_snapshot(terminal_session_id: str, uid: str, session_id: str) -> TerminalSessionSnapshot:
    async with AsyncSessionLocal() as db:
        return await terminal_session_manager.get_snapshot(db, terminal_session_id, uid, session_id)


def _configure_executor(executor, db, *, uid: str, session_id: str, tool_call_id: str):
    executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": STEP_TIMEOUT}}))
    executor.set_runtime_context(
        dispatch_context=DispatchContext(
            mode="interactive",
            source="test",
            uid=uid,
            session_id=session_id,
            profile=Profile(id=1, uid=uid, name="terminal-shell-workflow", configs={}),
            db=db,
            tool_call_id=tool_call_id,
        )
    )
    return executor


@pytest.mark.asyncio
async def test_terminal_shell_workflow_uses_real_interactive_executors(tmp_path):
    uid = TEST_UID
    session_id = TEST_SESSION_ID
    script_path = (tmp_path / "interactive_terminal.py").resolve()
    script_content = (
        "\n".join(
            [
                "import sys",
                "import time",
                "",
                'print("TTY_STDIN=" + str(sys.stdin.isatty()), flush=True)',
                'print("TTY_STDOUT=" + str(sys.stdout.isatty()), flush=True)',
                'print("TTY_STDERR=" + str(sys.stderr.isatty()), file=sys.stderr, flush=True)',
                "line = sys.stdin.readline()",
                f'print("{ECHO_MARKER}:" + line.rstrip("\\r\\n"), flush=True)',
                "time.sleep(60)",
            ]
        )
        + "\n"
    )
    await asyncio.to_thread(
        script_path.write_text,
        script_content,
        encoding="utf-8",
    )
    command = _interactive_python_command(script_path)
    coordinator = TerminalWorkerCoordinator()

    try:
        async with AsyncSessionLocal() as db:
            shell_executor = _configure_executor(
                ShellExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-shell-tool-call",
            )
            handoff = ShellInteractiveHandoffResult.model_validate(json.loads(await shell_executor.execute(command=command, execution_mode="interactive")))

        assert handoff.status is TerminalSessionStatus.STARTING
        async with AsyncSessionLocal() as db:
            terminal_session = await db.get(TerminalSession, handoff.terminal_session_id)
        assert terminal_session is not None
        assert terminal_session.command == command
        assert terminal_session.audit_record_id is None
        assert terminal_session.audit_execution_record_id is None

        coordinator.start()
        running_snapshot = await _wait_until(
            lambda: _get_snapshot(handoff.terminal_session_id, uid, session_id),
            lambda snapshot: snapshot.status is TerminalSessionStatus.RUNNING,
        )
        assert running_snapshot.status is TerminalSessionStatus.RUNNING
        async with AsyncSessionLocal() as db:
            stored_terminal = await db.get(TerminalSession, handoff.terminal_session_id)
        assert stored_terminal is not None
        assert stored_terminal.locked_by == coordinator.worker_id
        assert stored_terminal.process_identity is not None

        async with AsyncSessionLocal() as db:
            status_executor = _configure_executor(
                TerminalStatusExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-status-tool-call",
            )
            status_result = TerminalSessionSnapshot.model_validate(json.loads(await status_executor.execute(handoff.terminal_session_id)))
        assert status_result.status is TerminalSessionStatus.RUNNING

        async with AsyncSessionLocal() as db:
            resize_executor = _configure_executor(
                TerminalResizeExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-resize-tool-call",
            )
            resize_result = TerminalActionReceipt.model_validate(json.loads(await resize_executor.execute(handoff.terminal_session_id, columns=100, rows=40)))
        assert resize_result.action is TerminalAction.RESIZE
        assert resize_result.session_status is TerminalSessionStatus.RUNNING

        async with AsyncSessionLocal() as db:
            write_executor = _configure_executor(
                TerminalWriteExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-write-tool-call",
            )
            write_result = TerminalWriteResult.model_validate(
                json.loads(
                    await write_executor.execute(
                        handoff.terminal_session_id,
                        data="TERMINAL_WORKFLOW_INPUT\n",
                    )
                )
            )
        assert write_result.read_timed_out is False
        assert write_result.read_result is not None
        assert write_result.read_result.requested_offset == write_result.read_offset
        assert ECHO_MARKER in write_result.read_result.output

        async with AsyncSessionLocal() as db:
            read_executor = _configure_executor(
                TerminalReadExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-read-tool-call",
            )
            read_result = TerminalReadResult.model_validate(json.loads(await read_executor.execute(handoff.terminal_session_id, offset=0, max_bytes=65_536)))
        assert read_result.requested_offset == 0
        assert all(marker in read_result.output for marker in TTY_MARKERS)
        assert ECHO_MARKER in read_result.output

        async with AsyncSessionLocal() as db:
            close_executor = _configure_executor(
                TerminalCloseExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-close-tool-call",
            )
            close_result = TerminalActionReceipt.model_validate(json.loads(await close_executor.execute(handoff.terminal_session_id, force=True)))
        assert close_result.action is TerminalAction.CLOSE
        assert close_result.session_status is TerminalSessionStatus.EXITED

        async with AsyncSessionLocal() as db:
            exited_terminal = await db.get(TerminalSession, handoff.terminal_session_id)
        assert exited_terminal is not None
        assert exited_terminal.status is TerminalSessionStatus.EXITED
    finally:
        await asyncio.wait_for(coordinator.stop(), timeout=STEP_TIMEOUT)
