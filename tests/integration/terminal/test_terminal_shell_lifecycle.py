import asyncio
import json
import os
import sys

import psutil
import pytest

import app.core.terminal.session_runtime_core as terminal_runtime_core_module
from app.core.confirmation.lifecycle import stop_interactive_reply
from app.core.constants import ERR_TERMINAL_WORKER_STOPPED
from app.core.crud.task.background import background_task_crud
from app.core.crud.terminal.session import terminal_control_command_crud
from app.core.exceptions import ResourceNotFoundException
from app.core.i18n import t
from app.core.terminal import (
    ShellInteractiveHandoffResult,
    TerminalAction,
    TerminalReadResult,
    TerminalSessionStatus,
    TerminalWriteResult,
)
from app.core.terminal.manager import TerminalWorkerCoordinator
from app.core.tools.shell import ShellExecutor
from app.core.tools.terminal import TerminalToolExecutor
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.terminal_session import TerminalControlCommandStatus, TerminalSession
from app.providers.database import AsyncSessionLocal
from tests.integration.terminal import test_terminal_shell_workflow as shell_workflow_tests

pytestmark = shell_workflow_tests.pytestmark

STEP_TIMEOUT = shell_workflow_tests.STEP_TIMEOUT
TEST_UID = shell_workflow_tests.TEST_UID
TEST_SESSION_ID = shell_workflow_tests.TEST_SESSION_ID
_interactive_python_command = shell_workflow_tests._interactive_python_command
_wait_until = shell_workflow_tests._wait_until
_get_snapshot = shell_workflow_tests._get_snapshot
_configure_executor = shell_workflow_tests._configure_executor
_parse_read_result = shell_workflow_tests._parse_read_result
isolated_terminal_database = shell_workflow_tests.isolated_terminal_database


@pytest.fixture(autouse=True)
def _bind_terminal_database(isolated_terminal_database, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "AsyncSessionLocal", shell_workflow_tests.AsyncSessionLocal)


@pytest.mark.asyncio
async def test_stop_interactive_reply_deletes_terminal_during_identity_capture(tmp_path, monkeypatch):
    uid = TEST_UID
    session_id = TEST_SESSION_ID
    marker = "TERMINAL_IDENTITY_CAPTURE_RACE_5E2A"
    script_path = (tmp_path / "terminal_identity_capture_race.py").resolve()
    script_content = (
        "\n".join(
            [
                "import sys",
                f'print("{marker}", flush=True)',
                "sys.stdin.readline()",
            ]
        )
        + "\n"
    )
    await asyncio.to_thread(
        script_path.write_text,
        script_content,
        encoding="utf-8",
    )

    original_capture = terminal_runtime_core_module.capture_terminal_process_identity
    started = asyncio.Event()
    release = asyncio.Event()
    root_pid = None
    root_create_time = None

    async def delayed_capture(pid, previous_identity=None):
        nonlocal root_pid, root_create_time
        identity = await original_capture(pid, previous_identity)
        assert identity is not None
        root_pid = identity["root_pid"]
        root_create_time = identity["root_create_time"]
        started.set()
        await release.wait()
        return identity

    monkeypatch.setattr(
        terminal_runtime_core_module,
        "capture_terminal_process_identity",
        delayed_capture,
    )

    command = _interactive_python_command(script_path)
    coordinator = TerminalWorkerCoordinator()
    terminal_session_id = None
    runtime_task = None
    stop_task = None
    stop_started = asyncio.Event()

    async def stop_reply() -> int:
        async with AsyncSessionLocal() as db:
            stop_started.set()
            return await stop_interactive_reply(db, uid=uid, session_id=session_id)

    try:
        async with AsyncSessionLocal() as db:
            shell_executor = _configure_executor(
                ShellExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id="shell-identity-capture-race-tool-call",
            )
            handoff = ShellInteractiveHandoffResult.model_validate(json.loads(await shell_executor.execute(command=command, execution_mode="interactive")))

        terminal_session_id = handoff.terminal_session_id
        coordinator.start()
        await asyncio.wait_for(started.wait(), timeout=STEP_TIMEOUT)

        runtime_task = coordinator._runtime_tasks.get(terminal_session_id)
        assert runtime_task is not None
        async with AsyncSessionLocal() as db:
            stored_terminal = await db.get(TerminalSession, terminal_session_id)
            auto_watch = await background_task_crud.get_active_terminal_watch(
                db,
                uid,
                session_id,
                terminal_session_id,
                automatic_only=True,
            )
        assert stored_terminal is not None
        assert stored_terminal.status is TerminalSessionStatus.STARTING
        assert stored_terminal.locked_by == coordinator.worker_id
        assert stored_terminal.process_identity is None
        assert root_pid is not None
        assert root_create_time is not None
        root_process = psutil.Process(root_pid)
        assert root_process.is_running()
        assert root_process.status() != psutil.STATUS_ZOMBIE
        assert root_process.create_time() == root_create_time
        assert auto_watch is not None
        assert auto_watch.id is not None
        auto_watch_id = auto_watch.id

        stop_task = asyncio.create_task(stop_reply())
        await asyncio.wait_for(stop_started.wait(), timeout=STEP_TIMEOUT)
        await asyncio.sleep(0)
        assert not stop_task.done()
        release.set()
        assert await asyncio.wait_for(stop_task, timeout=STEP_TIMEOUT) == 0

        async with AsyncSessionLocal() as db:
            assert await db.get(TerminalSession, terminal_session_id) is None
            cancelled_watch = await db.get(BackgroundTask, auto_watch_id)
            assert cancelled_watch is not None
            assert cancelled_watch.status is BackgroundTaskStatus.CANCELLED
            assert cancelled_watch.auto_reply is False
            assert cancelled_watch.reply_status is BackgroundTaskReplyStatus.NONE
            assert (
                await background_task_crud.get_active_terminal_watch(
                    db,
                    uid,
                    session_id,
                    terminal_session_id,
                    automatic_only=True,
                )
                is None
            )

        async def runtime_released() -> bool:
            return runtime_task.done() and terminal_session_id not in coordinator._runtime_tasks and terminal_session_id not in coordinator._runtimes and terminal_session_id not in coordinator._owned_session_ids

        await _wait_until(runtime_released, lambda released: released)
        assert runtime_task.done()

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
    finally:
        release.set()
        if stop_task is not None and not stop_task.done():
            await asyncio.wait_for(stop_task, timeout=STEP_TIMEOUT)
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


@pytest.mark.asyncio
async def test_stop_interactive_reply_deletes_real_terminal_and_releases_runtime(tmp_path):
    uid = TEST_UID
    session_id = TEST_SESSION_ID
    terminal_state = "running"
    marker = "TERMINAL_STOP_WORKFLOW_RUNNING_9F51"
    script_path = (tmp_path / "terminal_stop_running.py").resolve()
    script_lines = [
        "import sys",
        f'print("{marker}", flush=True)',
    ]
    if terminal_state == "running":
        script_lines.append("sys.stdin.readline()")
    await asyncio.to_thread(
        script_path.write_text,
        "\n".join(script_lines) + "\n",
        encoding="utf-8",
    )
    command = _interactive_python_command(script_path)
    coordinator = TerminalWorkerCoordinator()
    terminal_session_id = None
    root_pid = None
    root_create_time = None
    runtime_task = None

    try:
        async with AsyncSessionLocal() as db:
            shell_executor = _configure_executor(
                ShellExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id=f"shell-stop-{terminal_state}-tool-call",
            )
            handoff = ShellInteractiveHandoffResult.model_validate(json.loads(await shell_executor.execute(command=command, execution_mode="interactive")))

        terminal_session_id = handoff.terminal_session_id
        coordinator.start()
        expected_status = TerminalSessionStatus.RUNNING if terminal_state == "running" else TerminalSessionStatus.EXITED
        snapshot = await _wait_until(
            lambda: _get_snapshot(terminal_session_id, uid, session_id),
            lambda value: value.status is expected_status and value.output_buffer.next_offset > 0,
        )
        assert snapshot.status is expected_status
        assert snapshot.output_buffer.next_offset > 0

        runtime_task = coordinator._runtime_tasks.get(terminal_session_id)
        assert runtime_task is not None
        assert terminal_session_id in coordinator._runtimes
        assert terminal_session_id in coordinator._owned_session_ids

        async with AsyncSessionLocal() as db:
            stored_terminal = await db.get(TerminalSession, terminal_session_id)
            auto_watch = await background_task_crud.get_active_terminal_watch(
                db,
                uid,
                session_id,
                terminal_session_id,
                automatic_only=True,
            )
        assert stored_terminal is not None
        assert stored_terminal.locked_by == coordinator.worker_id
        process_identity = stored_terminal.process_identity
        assert process_identity is not None
        root_pid = process_identity["root_pid"]
        root_create_time = process_identity["root_create_time"]
        assert isinstance(root_pid, int)
        assert isinstance(root_create_time, (int, float))
        assert auto_watch is not None
        assert auto_watch.id is not None
        auto_watch_id = auto_watch.id
        assert auto_watch.auto_reply is True

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
                    tool_call_id=f"shell-stop-{terminal_state}-read-tool-call-{read_attempt}",
                )
                return _parse_read_result(
                    await read_executor.execute(
                        "read",
                        terminal_session_id=terminal_session_id,
                        offset=0,
                        max_bytes=16_384,
                    )
                )

        read_result = await _wait_until(
            read_terminal_output,
            lambda result: marker in result.output,
        )
        assert read_result.output
        assert marker in read_result.output
        assert read_result.next_offset > read_result.start_offset
        async with AsyncSessionLocal() as db:
            read_commands = await terminal_control_command_crud.list_by_session(db, terminal_session_id)
        assert any(control_command.action is TerminalAction.READ and control_command.status is TerminalControlCommandStatus.SUCCEEDED for control_command in read_commands)

        async with AsyncSessionLocal() as db:
            assert await stop_interactive_reply(db, uid=uid, session_id=session_id) == 0

        async def runtime_released() -> bool:
            return runtime_task.done() and terminal_session_id not in coordinator._runtime_tasks and terminal_session_id not in coordinator._runtimes and terminal_session_id not in coordinator._owned_session_ids

        await _wait_until(runtime_released, lambda released: released)
        assert runtime_task.done()

        async with AsyncSessionLocal() as db:
            assert await db.get(TerminalSession, terminal_session_id) is None
            assert await terminal_control_command_crud.list_by_session(db, terminal_session_id) == []
            cancelled_watch = await db.get(BackgroundTask, auto_watch_id)
            assert cancelled_watch is not None
            assert cancelled_watch.status is BackgroundTaskStatus.CANCELLED
            assert cancelled_watch.auto_reply is False
            assert cancelled_watch.reply_status is BackgroundTaskReplyStatus.NONE
            assert (
                await background_task_crud.get_active_terminal_watch(
                    db,
                    uid,
                    session_id,
                    terminal_session_id,
                    automatic_only=True,
                )
                is None
            )

            read_after_stop = _configure_executor(
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id=f"shell-stop-{terminal_state}-read-after-stop",
            )
            with pytest.raises(ResourceNotFoundException):
                await read_after_stop.execute(
                    "read",
                    terminal_session_id=terminal_session_id,
                    read_output=False,
                )

        root_process = None
        try:
            root_process = psutil.Process(root_pid)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
        if root_process is not None:
            assert not root_process.is_running() or root_process.status() == psutil.STATUS_ZOMBIE or root_process.create_time() != root_create_time

        async with AsyncSessionLocal() as db:
            assert await stop_interactive_reply(db, uid=uid, session_id=session_id) == 0
    finally:
        await asyncio.wait_for(coordinator.stop(), timeout=STEP_TIMEOUT)


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.asyncio
async def test_terminal_natural_exit_persists_final_output_and_releases_runtime(tmp_path, exit_code):
    uid = TEST_UID
    session_id = TEST_SESSION_ID
    ready_marker = "TERMINAL_NATURAL_EXIT_READY_4C72"
    marker = f"TERMINAL_NATURAL_EXIT_{exit_code}_4C72"
    script_path = (tmp_path / f"terminal_natural_exit_{exit_code}.py").resolve()
    script_lines = [
        "import sys",
        f'print("{ready_marker}", flush=True)',
        "sys.stdin.readline()",
        "for index in range(600): print(f'{index:04d}:' + 'X' * 70, flush=True)",
        f'print("{marker}", flush=True)',
        f"sys.exit({exit_code})",
    ]
    await asyncio.to_thread(
        script_path.write_text,
        "\n".join(script_lines) + "\n",
        encoding="utf-8",
    )
    command = _interactive_python_command(script_path)
    coordinator = TerminalWorkerCoordinator()
    terminal_session_id = None
    root_pid = None
    root_create_time = None
    runtime_task = None
    runtime = None

    try:
        async with AsyncSessionLocal() as db:
            shell_executor = _configure_executor(
                ShellExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id=f"shell-natural-exit-{exit_code}-tool-call",
            )
            handoff = ShellInteractiveHandoffResult.model_validate(json.loads(await shell_executor.execute(command=command, execution_mode="interactive")))

        terminal_session_id = handoff.terminal_session_id
        coordinator.start()
        running_snapshot = await _wait_until(
            lambda: _get_snapshot(terminal_session_id, uid, session_id),
            lambda snapshot: snapshot.status is TerminalSessionStatus.RUNNING and snapshot.output_buffer.next_offset > 0,
        )
        assert running_snapshot.status is TerminalSessionStatus.RUNNING
        assert running_snapshot.output_buffer.next_offset > 0

        runtime_task = coordinator._runtime_tasks.get(terminal_session_id)
        runtime = coordinator._runtimes.get(terminal_session_id)
        assert runtime_task is not None
        assert runtime is not None
        assert terminal_session_id in coordinator._owned_session_ids

        async with AsyncSessionLocal() as db:
            stored_terminal = await db.get(TerminalSession, terminal_session_id)
        assert stored_terminal is not None
        process_identity = stored_terminal.process_identity
        assert process_identity is not None
        root_pid = process_identity["root_pid"]
        root_create_time = process_identity["root_create_time"]
        assert isinstance(root_pid, int)
        assert isinstance(root_create_time, (int, float))
        root_process = psutil.Process(root_pid)
        assert root_process.is_running()
        assert root_process.status() != psutil.STATUS_ZOMBIE
        assert root_process.create_time() == root_create_time

        async with AsyncSessionLocal() as db:
            write_executor = _configure_executor(
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id=f"shell-natural-exit-{exit_code}-write-tool-call",
            )
            write_result = TerminalWriteResult.model_validate(
                json.loads(
                    await write_executor.execute(
                        "write",
                        terminal_session_id=terminal_session_id,
                        data="\n",
                    )
                )
            )
        assert write_result.bytes_written == 1

        async def final_result():
            async with AsyncSessionLocal() as db:
                return await background_task_crud.get_latest_terminal_watch_final_result(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=1,
                    terminal_session_id=terminal_session_id,
                )

        final_task = await _wait_until(final_result, lambda task: task is not None)
        assert final_task is not None
        assert final_task.status is BackgroundTaskStatus.SUCCEEDED
        assert final_task.result is not None
        final_content = final_task.result["content"]
        assert final_content["event"] == "process_exit"
        assert final_content["session_status"] == TerminalSessionStatus.EXITED.value
        assert final_content["exit_code"] == exit_code
        assert final_content["eof"] is True
        assert marker in final_content["output"]
        assert final_content["output_truncated"] is True
        assert final_content["start_offset"] > final_content["after_offset"]
        assert final_content["next_offset"] > final_content["start_offset"]
        assert final_content["latest_offset"] == final_content["next_offset"]
        assert len(final_content["output"].encode("utf-8")) <= 16_384

        async def runtime_released() -> bool:
            return runtime_task.done() and terminal_session_id not in coordinator._runtime_tasks and terminal_session_id not in coordinator._runtimes and terminal_session_id not in coordinator._owned_session_ids

        await _wait_until(runtime_released, lambda released: released)
        assert runtime_task.done()
        assert runtime._driver is None
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
            assert await db.get(TerminalSession, terminal_session_id) is None
            assert await terminal_control_command_crud.list_by_session(db, terminal_session_id) == []
            read_after_exit = _configure_executor(
                TerminalToolExecutor(project_root=os.getcwd(), uid=uid),
                db,
                uid=uid,
                session_id=session_id,
                tool_call_id=f"shell-natural-exit-{exit_code}-read-after-exit",
            )
            with pytest.raises(ResourceNotFoundException):
                await read_after_exit.execute(
                    "read",
                    terminal_session_id=terminal_session_id,
                    read_output=False,
                )
    finally:
        await asyncio.wait_for(coordinator.stop(), timeout=STEP_TIMEOUT)
