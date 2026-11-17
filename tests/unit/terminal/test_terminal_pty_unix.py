import asyncio
import os
import re
import sys
from collections.abc import Callable

import psutil
import pytest

from app.core.terminal.pty_base import PtyProcessConfig
from app.core.terminal.pty_unix import LinuxPtyDriver

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux PTY tests require Linux",
)

POLL_INTERVAL = 0.01
WAIT_TIMEOUT = 10.0


def _config(
    script: str,
    *,
    columns: int = 80,
    rows: int = 24,
    output_capacity_bytes: int = 1_048_576,
    read_chunk_bytes: int = 65_536,
) -> PtyProcessConfig:
    return PtyProcessConfig(
        argv=(sys.executable, "-u", "-c", script),
        cwd=os.getcwd(),
        env=os.environ.copy(),
        columns=columns,
        rows=rows,
        output_capacity_bytes=output_capacity_bytes,
        read_chunk_bytes=read_chunk_bytes,
        close_grace_seconds=0.1,
    )


async def _force_close(driver: LinuxPtyDriver) -> None:
    await asyncio.wait_for(asyncio.shield(driver.close(force=True)), timeout=5.0)


async def _read_until(
    driver: LinuxPtyDriver,
    predicate: Callable[[bytes], bool],
    *,
    timeout: float = 5.0,
) -> tuple[bytes, int]:
    output = bytearray()
    offset = 0
    deadline = asyncio.get_running_loop().time() + timeout

    while True:
        result = driver.read_output(offset, 65_536)
        if result.truncated:
            offset = result.start_offset
        if result.data:
            output.extend(result.data)
            offset = result.next_offset
            value = bytes(output)
            if predicate(value):
                return value, offset
        if result.eof:
            raise AssertionError(f"PTY reached EOF before expected output: {bytes(output)!r}")

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for PTY output: {bytes(output)!r}")
        await asyncio.wait_for(
            asyncio.sleep(min(POLL_INTERVAL, remaining)),
            timeout=remaining,
        )


def _pid_is_alive(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


async def _wait_for_pid_exit(pid: int, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while _pid_is_alive(pid):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"process {pid} did not exit")
        await asyncio.wait_for(
            asyncio.sleep(min(POLL_INTERVAL, remaining)),
            timeout=remaining,
        )


async def test_linux_pty_connects_all_streams_and_applies_resize():
    script = (
        "import os,sys\n"
        "print(f'READY TTY={int(sys.stdin.isatty())}{int(sys.stdout.isatty())}{int(sys.stderr.isatty())}', flush=True)\n"
        "value = sys.stdin.readline()\n"
        "size = os.get_terminal_size(sys.stdout.fileno())\n"
        "print('RESPONSE:' + value.rstrip('\\r\\n'), flush=True)\n"
        "print(f'SIZE:{size.columns}x{size.lines}', file=sys.stderr, flush=True)\n"
    )
    driver = LinuxPtyDriver(_config(script))

    try:
        await asyncio.wait_for(driver.start(), timeout=5.0)
        assert driver.pid is not None

        await _read_until(driver, lambda output: b"READY TTY=111" in output)
        await asyncio.wait_for(driver.resize(100, 40), timeout=5.0)
        assert await asyncio.wait_for(driver.write("hello\n"), timeout=5.0) == 6
        assert await asyncio.wait_for(driver.wait(), timeout=WAIT_TIMEOUT) == 0

        result = driver.read_output(0, 65_536)
        assert result.eof
        assert b"RESPONSE:hello" in result.data
        assert b"SIZE:100x40" in result.data
        assert b"hello" in result.data

        snapshot = driver.resource_snapshot()
        assert snapshot.pid == driver.pid
        assert not snapshot.running
        assert snapshot.eof
        assert snapshot.exit_code == 0
        assert snapshot.retained_bytes == len(result.data)
        assert snapshot.dropped_bytes == 0

        await asyncio.wait_for(driver.close(), timeout=5.0)
        await asyncio.wait_for(driver.close(force=True), timeout=5.0)
    finally:
        await _force_close(driver)


async def test_linux_pty_drains_large_output_and_reports_truncation():
    payload = "0123456789ABCDEF" * 16_384
    script = "import sys\npayload = '0123456789ABCDEF' * 16384\nsys.stdout.write(payload)\nsys.stdout.flush()\n"
    capacity = 127
    driver = LinuxPtyDriver(
        _config(
            script,
            output_capacity_bytes=capacity,
            read_chunk_bytes=17,
        )
    )

    try:
        await asyncio.wait_for(driver.start(), timeout=5.0)
        assert await asyncio.wait_for(driver.wait(), timeout=WAIT_TIMEOUT) == 0

        snapshot = driver.resource_snapshot()
        assert snapshot.eof
        assert not snapshot.running
        assert snapshot.exit_code == 0
        assert snapshot.output_buffer.capacity_bytes == capacity
        assert snapshot.output_buffer.next_offset == len(payload)
        assert snapshot.retained_bytes == capacity
        assert snapshot.dropped_bytes == len(payload) - capacity

        truncated = driver.read_output(0, len(payload) + 1)
        assert truncated.eof
        assert truncated.truncated
        assert truncated.start_offset == len(payload) - capacity
        assert truncated.next_offset == len(payload)
        assert truncated.oldest_available_offset == len(payload) - capacity
        assert truncated.latest_offset == len(payload)
        assert truncated.data == payload[-capacity:].encode()

        retained = driver.read_output(len(payload) - capacity, capacity)
        assert not retained.truncated
        assert retained.data == payload[-capacity:].encode()
    finally:
        await _force_close(driver)


async def test_linux_pty_force_close_kills_process_group_and_is_idempotent():
    script = "import subprocess,sys,time\nchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\nprint(f'CHILD_PID:{child.pid}', flush=True)\ntime.sleep(60)\n"
    driver = LinuxPtyDriver(_config(script))

    try:
        await asyncio.wait_for(driver.start(), timeout=5.0)
        root_pid = driver.pid
        assert root_pid is not None

        output, _ = await _read_until(
            driver,
            lambda value: re.search(rb"CHILD_PID:(\d+)", value) is not None,
        )
        match = re.search(rb"CHILD_PID:(\d+)", output)
        assert match is not None
        child_pid = int(match.group(1))
        assert child_pid != root_pid
        child_process = psutil.Process(child_pid)
        assert child_process.ppid() == root_pid

        await _force_close(driver)
        await _force_close(driver)
        await _wait_for_pid_exit(root_pid)
        await _wait_for_pid_exit(child_pid)

        assert not _pid_is_alive(root_pid)
        assert not _pid_is_alive(child_pid)
        assert not driver.running
        assert driver.eof
        assert driver.exit_code is not None
    finally:
        await _force_close(driver)


async def test_linux_pty_rejects_operations_before_start():
    driver = LinuxPtyDriver(_config("import time; time.sleep(60)\n"))

    try:
        assert driver.pid is None
        assert not driver.running
        assert not driver.eof
        assert driver.exit_code is None
        with pytest.raises(RuntimeError):
            await driver.write("input")
        with pytest.raises(RuntimeError):
            await driver.resize(80, 24)
        with pytest.raises(RuntimeError):
            await driver.wait()
        await asyncio.wait_for(driver.close(force=True), timeout=5.0)
        await asyncio.wait_for(driver.close(force=True), timeout=5.0)
    finally:
        await _force_close(driver)


async def test_linux_pty_resize_input_boundaries():
    driver = LinuxPtyDriver(_config("import time; time.sleep(60)\n"))

    try:
        await asyncio.wait_for(driver.start(), timeout=5.0)
        await asyncio.wait_for(driver.resize(1, 1), timeout=5.0)
        await asyncio.wait_for(driver.resize(1_000, 1_000), timeout=5.0)

        for columns, rows in [
            (0, 1),
            (1, 0),
            (1_001, 1),
            (1, 1_001),
            (True, 1),
            (1, False),
        ]:
            with pytest.raises(ValueError):
                await driver.resize(columns, rows)
    finally:
        await _force_close(driver)
