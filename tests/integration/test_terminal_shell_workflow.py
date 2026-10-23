import asyncio
import json
import os
import platform
import shlex
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import psutil
import pytest

import app.core.terminal.session_runtime_commands as terminal_runtime_commands_module
import app.core.terminal.session_runtime_core as terminal_runtime_core_module
import app.core.terminal.session_runtime_state as terminal_runtime_state_module
import app.core.terminal.worker_coordinator as terminal_worker_module
from app.core.constants import (
    ERR_TERMINAL_COMMAND_REQUEST_CONFLICT,
    ERR_TERMINAL_COMMAND_TIMEOUT,
    ERR_TERMINAL_WORKER_STOPPED,
)
from app.core.crud.terminal.session import terminal_control_command_crud
from app.core.dispatchers.context import DispatchContext
from app.core.exceptions import ParameterException
from app.core.i18n import t
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
from app.core.utils.dispatcher.process_single_tool import get_tool_execution_status
from app.models.message import InternalMessage, MessageRole
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.terminal_session import TerminalControlCommandStatus, TerminalSession
from app.providers.database import AsyncSessionLocal
from app.providers.database.client import CancellationSafeAsyncSession

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
async def isolated_terminal_database(database_factory, monkeypatch):
    async with database_factory(
        foreign_keys=True,
        wal=True,
        session_class=CancellationSafeAsyncSession,
        name="terminal-shell-workflow.sqlite3",
    ) as factory:
        monkeypatch.setattr(sys.modules[__name__], "AsyncSessionLocal", factory)
        for module in (
            terminal_worker_module,
            terminal_runtime_core_module,
            terminal_runtime_state_module,
            terminal_runtime_commands_module,
        ):
            monkeypatch.setattr(module, "AsyncSessionLocal", factory)

        async with factory() as db:
            db.add(ChatSession(session_id=TEST_SESSION_ID, uid=TEST_UID))
            await db.commit()
        yield


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
            replayed_handoff = ShellInteractiveHandoffResult.model_validate(json.loads(await shell_executor.execute(command=command, execution_mode="interactive")))

        assert handoff.status is TerminalSessionStatus.STARTING
        assert replayed_handoff.terminal_session_id == handoff.terminal_session_id
        assert replayed_handoff.status is TerminalSessionStatus.STARTING
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

        write_data = "TERMINAL_WORKFLOW_INPUT\n"
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
                        data=write_data,
                    )
                )
            )
        assert write_result.action is TerminalAction.WRITE
        assert write_result.terminal_session_id == handoff.terminal_session_id
        assert write_result.duplicate is False
        assert write_result.bytes_written == len(write_data.encode("utf-8"))
        assert write_result.session_status is TerminalSessionStatus.RUNNING
        async with AsyncSessionLocal() as db:
            persisted_write = await terminal_control_command_crud.get_by_session_request(
                db,
                handoff.terminal_session_id,
                write_result.request_id,
            )
            recent_commands = await terminal_control_command_crud.list_recent_by_session(
                db,
                handoff.terminal_session_id,
                limit=10,
            )
        assert persisted_write is not None
        assert persisted_write.status is TerminalControlCommandStatus.SUCCEEDED
        assert persisted_write.result is not None
        assert persisted_write.result["bytes_written"] == write_result.bytes_written
        assert persisted_write.result["output_offset_before_write"] == write_result.read_offset
        assert not any(command.action is TerminalAction.READ for command in recent_commands)

        read_attempt = 0

        async def read_terminal_output() -> TerminalReadResult:
            nonlocal read_attempt
            read_attempt += 1
            async with AsyncSessionLocal() as db:
                read_executor = _configure_executor(
                    TerminalReadExecutor(project_root=os.getcwd(), uid=uid),
                    db,
                    uid=uid,
                    session_id=session_id,
                    tool_call_id=f"shell-workflow-read-after-write-{read_attempt}",
                )
                return TerminalReadResult.model_validate(
                    json.loads(
                        await read_executor.execute(
                            handoff.terminal_session_id,
                            offset=write_result.read_offset,
                            max_bytes=65_536,
                        )
                    )
                )

        read_result = await _wait_until(
            read_terminal_output,
            lambda result: ECHO_MARKER in result.output,
        )
        assert read_result.requested_offset == write_result.read_offset
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

        async with AsyncSessionLocal() as db:
            retained_read_executor = _configure_executor(
                TerminalReadExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-read-after-exit",
            )
            retained_read = TerminalReadResult.model_validate(
                json.loads(
                    await retained_read_executor.execute(
                        handoff.terminal_session_id,
                        offset=0,
                        max_bytes=65_536,
                    )
                )
            )
        assert retained_read.eof is True
        assert all(marker in retained_read.output for marker in TTY_MARKERS)
        assert ECHO_MARKER in retained_read.output

        async with AsyncSessionLocal() as db:
            repeated_close_executor = _configure_executor(
                TerminalCloseExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-close-tool-call",
            )
            repeated_close = TerminalActionReceipt.model_validate(
                json.loads(
                    await repeated_close_executor.execute(
                        handoff.terminal_session_id,
                        force=True,
                    )
                )
            )
        assert repeated_close.action is TerminalAction.CLOSE
        assert repeated_close.session_status is TerminalSessionStatus.EXITED
    finally:
        await asyncio.wait_for(coordinator.stop(), timeout=STEP_TIMEOUT)


@pytest.mark.asyncio
async def test_terminal_write_timeout_preserves_pending_command_for_idempotent_completion(tmp_path):
    uid = TEST_UID
    session_id = TEST_SESSION_ID
    script_path = (tmp_path / "terminal_write_timeout.py").resolve()
    script_content = "import sys\nimport time\nsys.stdin.readline()\ntime.sleep(60)\n"
    await asyncio.to_thread(
        script_path.write_text,
        script_content,
        encoding="utf-8",
    )
    command = _interactive_python_command(script_path)
    coordinator = TerminalWorkerCoordinator()
    write_data = "delayed input\n"
    tool_call_id = "write-timeout-confirmation-call"

    try:
        async with AsyncSessionLocal() as db:
            shell_executor = _configure_executor(
                ShellExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-shell-tool-call",
            )
            handoff = ShellInteractiveHandoffResult.model_validate(
                json.loads(
                    await shell_executor.execute(
                        command=command,
                        execution_mode="interactive",
                    )
                )
            )
        assert handoff.status is TerminalSessionStatus.STARTING

        async with AsyncSessionLocal() as db:
            write_executor = _configure_executor(
                TerminalWriteExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id=tool_call_id,
            )
            write_executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": 0.05}}))
            unknown_content = await write_executor.execute(handoff.terminal_session_id, data=write_data)

        unknown_result = json.loads(unknown_content)
        assert set(unknown_result) == {
            "status",
            "tool_name",
            "terminal_session_id",
            "request_id",
            "error_code",
            "error",
        }
        assert unknown_result["status"] == "execution_unknown"
        assert unknown_result["tool_name"] == "terminal_write"
        assert unknown_result["terminal_session_id"] == handoff.terminal_session_id
        assert unknown_result["error_code"] == ERR_TERMINAL_COMMAND_TIMEOUT
        assert isinstance(unknown_result["error"], str) and unknown_result["error"]
        unknown_tool_result = InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=tool_call_id,
            content=unknown_content,
        )
        assert get_tool_execution_status(unknown_tool_result) == "execution_unknown"

        async with AsyncSessionLocal() as db:
            pending_status_executor = _configure_executor(
                TerminalStatusExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-pending-status-call",
            )
            pending_status = json.loads(
                await pending_status_executor.execute(
                    handoff.terminal_session_id,
                    request_id=unknown_result["request_id"],
                )
            )
        assert pending_status["status"] == TerminalSessionStatus.STARTING.value
        assert pending_status["write_request"] == {
            "request_id": unknown_result["request_id"],
            "command_status": "pending",
            "result": None,
            "error": None,
        }

        async with AsyncSessionLocal() as db:
            pending_write = await terminal_control_command_crud.get_by_session_request(
                db,
                handoff.terminal_session_id,
                unknown_result["request_id"],
            )
            recent_commands = await terminal_control_command_crud.list_recent_by_session(
                db,
                handoff.terminal_session_id,
                limit=10,
            )
        assert pending_write is not None
        assert pending_write.status is TerminalControlCommandStatus.PENDING
        assert pending_write.action is TerminalAction.WRITE
        assert pending_write.id is not None
        command_id = pending_write.id
        assert len(recent_commands) == 1
        assert recent_commands[0].id == command_id
        assert recent_commands[0].action is TerminalAction.WRITE
        assert not any(item.action is TerminalAction.READ for item in recent_commands)

        async with AsyncSessionLocal() as db:
            pending_retry_executor = _configure_executor(
                TerminalWriteExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-pending-retry-call",
            )
            pending_retry_executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": 0.05}}))
            pending_retry_content = await pending_retry_executor.execute(
                handoff.terminal_session_id,
                data=write_data,
                request_id=unknown_result["request_id"],
            )
        pending_retry_result = json.loads(pending_retry_content)
        assert pending_retry_result["status"] == "execution_unknown"
        assert pending_retry_result["request_id"] == unknown_result["request_id"]
        async with AsyncSessionLocal() as db:
            pending_retry_commands = await terminal_control_command_crud.list_recent_by_session(
                db,
                handoff.terminal_session_id,
                limit=10,
            )
        assert len(pending_retry_commands) == 1
        assert pending_retry_commands[0].id == command_id
        assert pending_retry_commands[0].action is TerminalAction.WRITE
        assert not any(item.action is TerminalAction.READ for item in pending_retry_commands)

        coordinator.start()
        async with AsyncSessionLocal() as db:
            worker_result = await terminal_session_manager.wait_for_command_result(
                db,
                command_id,
                STEP_TIMEOUT,
            )
        assert worker_result["bytes_written"] == len(write_data.encode("utf-8"))
        async with AsyncSessionLocal() as db:
            completed_write = await terminal_control_command_crud.get_by_session_request(
                db,
                handoff.terminal_session_id,
                unknown_result["request_id"],
            )
        assert completed_write is not None
        assert completed_write.status is TerminalControlCommandStatus.SUCCEEDED
        assert completed_write.result is not None
        assert completed_write.result["bytes_written"] == len(write_data.encode("utf-8"))
        assert completed_write.result["output_offset_before_write"] == worker_result["output_offset_before_write"]

        async with AsyncSessionLocal() as db:
            completed_status_executor = _configure_executor(
                TerminalStatusExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-completed-status-call",
            )
            completed_status = json.loads(
                await completed_status_executor.execute(
                    handoff.terminal_session_id,
                    request_id=unknown_result["request_id"],
                )
            )
        completed_status_write = completed_status["write_request"]
        assert completed_status_write["command_status"] == "succeeded"
        assert completed_status_write["result"]["bytes_written"] == completed_write.result["bytes_written"]
        assert completed_status_write["result"]["output_offset_before_write"] == completed_write.result["output_offset_before_write"]
        assert completed_status_write["error"] is None

        async with AsyncSessionLocal() as db:
            replay_executor = _configure_executor(
                TerminalWriteExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-replay-call",
            )
            replay_result = TerminalWriteResult.model_validate(
                json.loads(
                    await replay_executor.execute(
                        handoff.terminal_session_id,
                        data=write_data,
                        request_id=unknown_result["request_id"],
                    )
                )
            )
        assert replay_result.action is TerminalAction.WRITE
        assert replay_result.duplicate is True
        assert replay_result.request_id == unknown_result["request_id"]
        assert replay_result.bytes_written == len(write_data.encode("utf-8"))
        assert replay_result.read_offset == completed_write.result["output_offset_before_write"]

        async with AsyncSessionLocal() as db:
            conflict_executor = _configure_executor(
                TerminalWriteExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-conflicting-replay-call",
            )
            with pytest.raises(ParameterException) as conflict_error:
                await conflict_executor.execute(
                    handoff.terminal_session_id,
                    data="different input\n",
                    request_id=unknown_result["request_id"],
                )
        assert conflict_error.value.message == ERR_TERMINAL_COMMAND_REQUEST_CONFLICT

        async with AsyncSessionLocal() as db:
            unchanged_original = await terminal_control_command_crud.get_by_session_request(
                db,
                handoff.terminal_session_id,
                unknown_result["request_id"],
            )
            commands_after_conflict = await terminal_control_command_crud.list_recent_by_session(
                db,
                handoff.terminal_session_id,
                limit=10,
            )
        assert unchanged_original is not None
        assert unchanged_original.status is TerminalControlCommandStatus.SUCCEEDED
        assert unchanged_original.payload["data"] == write_data
        assert unchanged_original.result == completed_write.result
        assert len(commands_after_conflict) == 1
        assert commands_after_conflict[0].id == command_id
        assert commands_after_conflict[0].action is TerminalAction.WRITE

        async with AsyncSessionLocal() as db:
            fresh_write_executor = _configure_executor(
                TerminalWriteExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-fresh-operation-call",
            )
            fresh_write = TerminalWriteResult.model_validate(
                json.loads(
                    await fresh_write_executor.execute(
                        handoff.terminal_session_id,
                        data=write_data,
                    )
                )
            )
        assert fresh_write.duplicate is False
        assert fresh_write.request_id != unknown_result["request_id"]

        async with AsyncSessionLocal() as db:
            final_commands = await terminal_control_command_crud.list_recent_by_session(
                db,
                handoff.terminal_session_id,
                limit=10,
            )
        assert len(final_commands) == 2
        assert {item.request_id for item in final_commands} == {
            unknown_result["request_id"],
            fresh_write.request_id,
        }
        assert all(item.action is TerminalAction.WRITE for item in final_commands)
        assert not any(item.action is TerminalAction.READ for item in final_commands)
    finally:
        await asyncio.wait_for(coordinator.stop(), timeout=STEP_TIMEOUT)


@pytest.mark.asyncio
async def test_terminal_worker_stop_marks_real_unaudited_shell_session_lost_and_kills_process(tmp_path):
    uid = TEST_UID
    session_id = TEST_SESSION_ID
    script_path = (tmp_path / "terminal_worker_stop.py").resolve()
    await asyncio.to_thread(
        script_path.write_text,
        'import time\nprint("WORKER_STOP_READY", flush=True)\ntime.sleep(60)\n',
        encoding="utf-8",
    )
    command = _interactive_python_command(script_path)
    coordinator = TerminalWorkerCoordinator()
    process_identity = None

    try:
        async with AsyncSessionLocal() as db:
            shell_executor = _configure_executor(
                ShellExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-worker-stop-tool-call",
            )
            handoff = ShellInteractiveHandoffResult.model_validate(
                json.loads(
                    await shell_executor.execute(
                        command=command,
                        execution_mode="interactive",
                    )
                )
            )

        assert handoff.status is TerminalSessionStatus.STARTING
        coordinator.start()
        running_snapshot = await _wait_until(
            lambda: _get_snapshot(handoff.terminal_session_id, uid, session_id),
            lambda snapshot: snapshot.status is TerminalSessionStatus.RUNNING,
        )
        assert running_snapshot.status is TerminalSessionStatus.RUNNING

        async with AsyncSessionLocal() as db:
            stored_terminal = await db.get(TerminalSession, handoff.terminal_session_id)
        assert stored_terminal is not None
        process_identity = stored_terminal.process_identity
        assert process_identity is not None
        assert stored_terminal.audit_record_id is None
        assert stored_terminal.audit_execution_record_id is None

        await asyncio.wait_for(coordinator.stop(), timeout=STEP_TIMEOUT)

        async with AsyncSessionLocal() as db:
            stopped_terminal = await db.get(TerminalSession, handoff.terminal_session_id)
        assert stopped_terminal is not None
        assert stopped_terminal.status is TerminalSessionStatus.LOST
        assert stopped_terminal.failure_reason == t(ERR_TERMINAL_WORKER_STOPPED)
        assert stopped_terminal.locked_by is None
        assert stopped_terminal.lock_until is None

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
