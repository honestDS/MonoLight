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
from pydantic import ValidationError

import app.core.terminal.session_runtime_commands as terminal_runtime_commands_module
import app.core.terminal.session_runtime_core as terminal_runtime_core_module
import app.core.terminal.session_runtime_state as terminal_runtime_state_module
import app.core.terminal.worker_coordinator as terminal_worker_module
import app.core.tools.terminal as terminal_tool_module
from app.core.constants import (
    ERR_TERMINAL_COMMAND_REQUEST_CONFLICT,
    ERR_TERMINAL_COMMAND_TIMEOUT,
)
from app.core.crud.terminal.session import terminal_control_command_crud
from app.core.dispatchers.context import DispatchContext
from app.core.exceptions import ParameterException, ResourceNotFoundException
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
from app.core.tools.terminal import TerminalToolExecutor
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
            terminal_tool_module,
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


def _parse_read_result(content: str) -> TerminalReadResult:
    payload = json.loads(content)
    return TerminalReadResult.model_validate({field: payload[field] for field in TerminalReadResult.model_fields})


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
        root_pid = process_identity["root_pid"]
        root_create_time = process_identity["root_create_time"]
        root_process = psutil.Process(root_pid)
        assert root_process.is_running()
        assert root_process.status() != psutil.STATUS_ZOMBIE
        runtime_task = coordinator._runtime_tasks.get(handoff.terminal_session_id)
        assert runtime_task is not None
        assert handoff.terminal_session_id in coordinator._runtimes
        assert handoff.terminal_session_id in coordinator._owned_session_ids

        async with AsyncSessionLocal() as db:
            status_executor = _configure_executor(
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-status-tool-call",
            )
            status_result = TerminalSessionSnapshot.model_validate(
                json.loads(
                    await status_executor.execute(
                        "read",
                        terminal_session_id=handoff.terminal_session_id,
                        read_output=False,
                    )
                )
            )
        assert status_result.status is TerminalSessionStatus.RUNNING

        async with AsyncSessionLocal() as db:
            resize_executor = _configure_executor(
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-resize-tool-call",
            )
            resize_result = TerminalActionReceipt.model_validate(
                json.loads(
                    await resize_executor.execute(
                        "resize",
                        terminal_session_id=handoff.terminal_session_id,
                        columns=100,
                        rows=40,
                    )
                )
            )
        assert resize_result.action is TerminalAction.RESIZE
        assert resize_result.session_status is TerminalSessionStatus.RUNNING

        write_data = "TERMINAL_WORKFLOW_INPUT\n"
        async with AsyncSessionLocal() as db:
            write_executor = _configure_executor(
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-write-tool-call",
            )
            write_result = TerminalWriteResult.model_validate(
                json.loads(
                    await write_executor.execute(
                        "write",
                        terminal_session_id=handoff.terminal_session_id,
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
                    TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                    db,
                    uid=uid,
                    session_id=session_id,
                    tool_call_id=f"shell-workflow-read-after-write-{read_attempt}",
                )
                return _parse_read_result(
                    await read_executor.execute(
                        "read",
                        terminal_session_id=handoff.terminal_session_id,
                        offset=write_result.read_offset,
                        max_bytes=16_384,
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
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-close-tool-call",
            )
            close_result = json.loads(
                await close_executor.execute(
                    "close",
                    terminal_session_id=handoff.terminal_session_id,
                    force=True,
                )
            )
        assert close_result["action"] == TerminalAction.CLOSE.value
        assert close_result["terminal_session_id"] == handoff.terminal_session_id
        assert close_result["session_status"] == TerminalSessionStatus.EXITED.value
        assert isinstance(close_result["exit_code"], int)
        assert isinstance(close_result["output"], str)
        assert all(marker in close_result["output"] for marker in TTY_MARKERS)
        assert ECHO_MARKER in close_result["output"]
        assert close_result["start_offset"] <= close_result["next_offset"] <= close_result["latest_offset"]
        assert close_result["output_truncated"] is False
        assert close_result["resources_released"] is True

        async def runtime_released() -> bool:
            return runtime_task.done() and handoff.terminal_session_id not in coordinator._runtime_tasks and handoff.terminal_session_id not in coordinator._runtimes and handoff.terminal_session_id not in coordinator._owned_session_ids

        await _wait_until(runtime_released, lambda released: released)
        assert runtime_task.done()
        assert coordinator._task is not None
        assert not coordinator._task.done()

        async def root_process_released() -> bool:
            try:
                process = psutil.Process(root_pid)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                return True
            try:
                return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE or process.create_time() != root_create_time
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                return True

        assert await _wait_until(root_process_released, lambda released: released)

        async with AsyncSessionLocal() as db:
            assert await db.get(TerminalSession, handoff.terminal_session_id) is None
            assert await terminal_control_command_crud.list_by_session(db, handoff.terminal_session_id) == []

        async with AsyncSessionLocal() as db:
            read_after_close = _configure_executor(
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-read-after-close",
            )
            with pytest.raises(ResourceNotFoundException):
                await read_after_close.execute(
                    "read",
                    terminal_session_id=handoff.terminal_session_id,
                    offset=0,
                    max_bytes=16_384,
                )

            close_after_close = _configure_executor(
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-workflow-close-after-close",
            )
            with pytest.raises(ResourceNotFoundException):
                await close_after_close.execute(
                    "close",
                    terminal_session_id=handoff.terminal_session_id,
                    force=True,
                )
    finally:
        await asyncio.wait_for(coordinator.stop(), timeout=STEP_TIMEOUT)


@pytest.mark.asyncio
async def test_terminal_read_requires_ai_selected_pagination(tmp_path):
    uid = TEST_UID
    session_id = TEST_SESSION_ID
    marker = "TERMINAL_READ_PAGING_EOF_MARKER"
    script_path = (tmp_path / "terminal_read_paging.py").resolve()
    script_content = f'import sys\nimport time\nfor index in range(128):\n    chunk = "".join(f"{{index:03d}}:{{offset:03d}}:Ab9Xy7" for offset in range(36))\n    sys.stdout.write(chunk + "\\n")\n    sys.stdout.flush()\n    time.sleep(0.01)\nprint("{marker}", flush=True)\nsys.stdin.readline()\n'
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
                tool_call_id="shell-read-paging-tool-call",
            )
            handoff = ShellInteractiveHandoffResult.model_validate(json.loads(await shell_executor.execute(command=command, execution_mode="interactive")))

        coordinator.start()
        running_snapshot = await _wait_until(
            lambda: _get_snapshot(handoff.terminal_session_id, uid, session_id),
            lambda snapshot: snapshot.status is TerminalSessionStatus.RUNNING and snapshot.output_buffer.next_offset >= 49_152,
        )
        assert running_snapshot.status is TerminalSessionStatus.RUNNING
        assert running_snapshot.output_buffer.next_offset >= 49_152
        latest_offset = running_snapshot.output_buffer.next_offset

        async def read_page(tool_call_id: str, **kwargs):
            async with AsyncSessionLocal() as db:
                read_executor = _configure_executor(
                    TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                    db,
                    uid=uid,
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                )
                payload = json.loads(
                    await read_executor.execute(
                        "read",
                        terminal_session_id=handoff.terminal_session_id,
                        **kwargs,
                    )
                )
            return payload, TerminalReadResult.model_validate({field: payload[field] for field in TerminalReadResult.model_fields})

        async def persisted_commands():
            async with AsyncSessionLocal() as db:
                return await terminal_control_command_crud.list_by_session(db, handoff.terminal_session_id)

        commands = await persisted_commands()
        assert len(commands) == 0

        first_payload, first_page = await read_page("ai-read-page-0")
        assert first_payload["status"] == TerminalSessionStatus.RUNNING.value
        assert first_payload["exit_code"] is None
        assert first_page.requested_offset == first_page.start_offset == 0
        assert first_page.eof is False
        assert first_page.next_offset - first_page.start_offset == 16_384
        assert first_page.next_offset < latest_offset
        commands = await persisted_commands()
        assert len(commands) == 1
        assert sum(item.action is TerminalAction.READ for item in commands) == 1
        assert sum(item.action is TerminalAction.WRITE for item in commands) == 0

        selected_payload, selected_page = await read_page(
            "ai-read-page-32768",
            offset=32_768,
            max_bytes=512,
        )
        selected_bytes = selected_page.output.encode("utf-8")
        assert selected_payload["status"] == TerminalSessionStatus.RUNNING.value
        assert selected_page.requested_offset == selected_page.start_offset == 32_768
        assert selected_page.next_offset == 32_768 + len(selected_bytes) == 33_280
        commands = await persisted_commands()
        assert len(selected_bytes) == 512
        assert len(commands) == 2
        assert sum(item.action is TerminalAction.READ for item in commands) == 2
        assert sum(item.action is TerminalAction.WRITE for item in commands) == 0

        _, repeated_page = await read_page(
            "ai-read-page-32768-repeat",
            offset=32_768,
            max_bytes=512,
        )
        assert repeated_page.requested_offset == selected_page.requested_offset
        assert repeated_page.start_offset == selected_page.start_offset
        assert repeated_page.next_offset == selected_page.next_offset
        assert repeated_page.output == selected_page.output
        commands = await persisted_commands()
        assert len(commands) == 3
        assert sum(item.action is TerminalAction.READ for item in commands) == 3
        assert sum(item.action is TerminalAction.WRITE for item in commands) == 0

        with pytest.raises(ValidationError):
            await read_page("ai-read-page-over-limit", offset=32_768, max_bytes=16_385)
        commands = await persisted_commands()
        assert len(commands) == 3
        assert sum(item.action is TerminalAction.READ for item in commands) == 3
        assert sum(item.action is TerminalAction.WRITE for item in commands) == 0
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
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id=tool_call_id,
            )
            write_executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": 0.05}}))
            unknown_content = await write_executor.execute(
                "write",
                terminal_session_id=handoff.terminal_session_id,
                data=write_data,
            )

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
        assert unknown_result["tool_name"] == "terminal_tool"
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
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-pending-status-call",
            )
            pending_status = json.loads(
                await pending_status_executor.execute(
                    "read",
                    terminal_session_id=handoff.terminal_session_id,
                    request_id=unknown_result["request_id"],
                    read_output=False,
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
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-pending-retry-call",
            )
            pending_retry_executor.set_config(ProfileConfig.model_validate({"tool": {"tool_timeout": 0.05}}))
            pending_retry_content = await pending_retry_executor.execute(
                "write",
                terminal_session_id=handoff.terminal_session_id,
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
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-completed-status-call",
            )
            completed_status = json.loads(
                await completed_status_executor.execute(
                    "read",
                    terminal_session_id=handoff.terminal_session_id,
                    request_id=unknown_result["request_id"],
                    read_output=False,
                )
            )
        completed_status_write = completed_status["write_request"]
        assert completed_status_write["command_status"] == "succeeded"
        assert completed_status_write["result"]["bytes_written"] == completed_write.result["bytes_written"]
        assert completed_status_write["result"]["output_offset_before_write"] == completed_write.result["output_offset_before_write"]
        assert completed_status_write["error"] is None

        async with AsyncSessionLocal() as db:
            replay_executor = _configure_executor(
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-replay-call",
            )
            replay_result = TerminalWriteResult.model_validate(
                json.loads(
                    await replay_executor.execute(
                        "write",
                        terminal_session_id=handoff.terminal_session_id,
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
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-conflicting-replay-call",
            )
            with pytest.raises(ParameterException) as conflict_error:
                await conflict_executor.execute(
                    "write",
                    terminal_session_id=handoff.terminal_session_id,
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
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="write-timeout-fresh-operation-call",
            )
            fresh_write = TerminalWriteResult.model_validate(
                json.loads(
                    await fresh_write_executor.execute(
                        "write",
                        terminal_session_id=handoff.terminal_session_id,
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
