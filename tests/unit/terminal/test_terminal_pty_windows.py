import asyncio
import os
import re
import sys
import time

import psutil
import pytest

from app.core.terminal.pty_base import PtyProcessConfig
from app.core.terminal.pty_windows import WindowsPtyDriver

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows ConPTY tests require win32",
)


async def _wait_for_output(
    driver: WindowsPtyDriver,
    marker: bytes,
    timeout: float = 10.0,
) -> bytes:
    async def poll() -> bytes:
        deadline = time.monotonic() + timeout
        offset = 0
        output = bytearray()
        while time.monotonic() < deadline:
            result = driver.read_output(offset, 65_536)
            if result.data:
                output.extend(result.data)
                offset = result.next_offset
            if marker in output:
                return bytes(output)
            await asyncio.sleep(0.02)
        raise AssertionError(f"Timed out waiting for {marker!r}")

    return await asyncio.wait_for(poll(), timeout=timeout + 1.0)


async def _wait_for_process_state(
    pid: int,
    expected_running: bool,
    timeout: float = 10.0,
) -> None:
    async def poll() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _process_is_running(pid) is expected_running:
                return
            await asyncio.sleep(0.05)
        assert _process_is_running(pid) is expected_running

    await asyncio.wait_for(poll(), timeout=timeout + 1.0)


def _process_is_running(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        if process.status() in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}:
            return False
        return process.is_running()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


def _config(script: str, *, output_capacity_bytes: int = 1_048_576) -> PtyProcessConfig:
    return PtyProcessConfig(
        argv=(sys.executable, "-u", "-c", script),
        cwd=os.getcwd(),
        env=os.environ.copy(),
        output_capacity_bytes=output_capacity_bytes,
    )


@pytest.mark.asyncio
async def test_windows_pty_interactive_resize_write_and_snapshot() -> None:
    script = "import os\nimport sys\nprint('READY', flush=True)\nline = input()\nprint(f'ECHO:{line}', flush=True)\nsize = os.get_terminal_size()\nprint(f'SIZE:{size.columns}x{size.lines}', flush=True)\n"
    driver = WindowsPtyDriver(_config(script))
    try:
        await asyncio.wait_for(driver.start(), timeout=10.0)
        pid = driver.pid
        assert isinstance(pid, int)
        assert pid > 0
        assert psutil.Process(pid).pid == pid

        await _wait_for_output(driver, b"READY")
        await asyncio.wait_for(driver.resize(100, 40), timeout=5.0)
        written = await asyncio.wait_for(driver.write("hello\n"), timeout=5.0)
        assert written == len(b"hello\n")
        exit_code = await asyncio.wait_for(driver.wait(), timeout=15.0)
        output = await _wait_for_output(driver, b"SIZE:100x40")

        assert exit_code == 0
        assert b"READY" in output
        assert b"ECHO:hello" in output
        assert b"SIZE:100x40" in output

        snapshot = driver.resource_snapshot()
        assert snapshot.pid == pid
        assert snapshot.running is False
        assert snapshot.eof is True
        assert snapshot.exit_code == 0
        assert snapshot.retained_bytes == (snapshot.output_buffer.next_offset - snapshot.output_buffer.oldest_offset)
        assert snapshot.dropped_bytes == snapshot.output_buffer.oldest_offset
    finally:
        await asyncio.wait_for(driver.close(force=True), timeout=15.0)

    await asyncio.wait_for(driver.close(force=True), timeout=5.0)


@pytest.mark.asyncio
async def test_windows_pty_bounded_output_continues_draining() -> None:
    capacity = 64
    script = "import sys; sys.stdout.write('X' * 4096); sys.stdout.flush()"
    driver = WindowsPtyDriver(
        _config(script, output_capacity_bytes=capacity),
    )
    try:
        await asyncio.wait_for(driver.start(), timeout=10.0)
        exit_code = await asyncio.wait_for(driver.wait(), timeout=15.0)

        snapshot = driver.resource_snapshot()
        result = driver.read_output(0, capacity * 2)

        assert exit_code == 0
        assert snapshot.eof is True
        assert snapshot.running is False
        assert snapshot.retained_bytes == capacity
        assert snapshot.dropped_bytes > 0
        assert result.truncated is True
        assert len(result.data) <= capacity
        assert len(result.data) == capacity
    finally:
        await asyncio.wait_for(driver.close(force=True), timeout=15.0)


@pytest.mark.asyncio
async def test_windows_pty_force_close_terminates_process_tree() -> None:
    script = "import subprocess\nimport sys\nimport time\nchild = subprocess.Popen([sys.executable, '-u', '-c', 'import time; time.sleep(60)'])\nprint(f'CHILD_PID:{child.pid}', flush=True)\ntime.sleep(60)\n"
    driver = WindowsPtyDriver(_config(script))
    try:
        await asyncio.wait_for(driver.start(), timeout=10.0)
        root_pid = driver.pid
        assert isinstance(root_pid, int)
        assert root_pid > 0

        output = await _wait_for_output(driver, b"CHILD_PID:", timeout=10.0)
        match = re.search(rb"CHILD_PID:(\d+)", output)
        assert match is not None
        child_pid = int(match.group(1))
        assert child_pid > 0
        await _wait_for_process_state(child_pid, True, timeout=5.0)

        await asyncio.wait_for(driver.close(force=True), timeout=15.0)
        await _wait_for_process_state(root_pid, False, timeout=10.0)
        await _wait_for_process_state(child_pid, False, timeout=10.0)
    finally:
        await asyncio.wait_for(driver.close(force=True), timeout=15.0)

    await asyncio.wait_for(driver.close(force=True), timeout=5.0)


@pytest.mark.asyncio
async def test_windows_pty_force_close_resolves_wait_with_exit_status() -> None:
    script = "import time; time.sleep(60)"
    driver = WindowsPtyDriver(_config(script))
    wait_task: asyncio.Task[int] | None = None
    try:
        await asyncio.wait_for(driver.start(), timeout=10.0)
        wait_task = asyncio.create_task(driver.wait())

        await asyncio.wait_for(driver.close(force=True), timeout=15.0)
        exit_code = await asyncio.wait_for(wait_task, timeout=15.0)
        snapshot = driver.resource_snapshot()

        assert isinstance(exit_code, int)
        assert snapshot.eof is True
        assert snapshot.running is False
        assert snapshot.exit_code == exit_code
    finally:
        await asyncio.wait_for(driver.close(force=True), timeout=15.0)
        if wait_task is not None and not wait_task.done():
            await asyncio.wait_for(wait_task, timeout=15.0)

    await asyncio.wait_for(driver.close(force=True), timeout=5.0)


@pytest.mark.asyncio
async def test_windows_pty_rejects_unstarted_and_boundary_operations() -> None:
    script = "import time; time.sleep(60)"
    driver = WindowsPtyDriver(_config(script))

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(driver.write("hello"), timeout=5.0)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(driver.resize(80, 24), timeout=5.0)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(driver.wait(), timeout=5.0)
    with pytest.raises(ValueError):
        await asyncio.wait_for(driver.write(""), timeout=5.0)
    with pytest.raises(ValueError):
        await asyncio.wait_for(driver.resize(0, 24), timeout=5.0)
