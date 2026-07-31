"""Linux PTY process driver."""

import asyncio
import errno
import os
import signal as signal_module
import struct
import subprocess
import sys

from app.core.constants import (
    ERR_TERMINAL_PTY_ALREADY_STARTED,
    ERR_TERMINAL_PTY_CLOSED,
    ERR_TERMINAL_PTY_DIMENSIONS_INVALID,
    ERR_TERMINAL_PTY_DRAIN_UNAVAILABLE,
    ERR_TERMINAL_PTY_INPUT_INVALID,
    ERR_TERMINAL_PTY_NOT_STARTED,
    ERR_TERMINAL_PTY_PLATFORM_MISMATCH,
    ERR_TERMINAL_PTY_SIGNAL_INVALID,
    ERR_TERMINAL_PTY_STATE_INVALID,
    ERR_TERMINAL_PTY_WRITE_STALLED,
)
from app.core.i18n import t
from app.core.terminal.pty_base import PtyDriver, PtyProcessConfig
from app.core.terminal.schemas import TerminalSignal

if sys.platform.startswith("linux"):
    import fcntl
    import pty
    import termios


class LinuxPtyDriver(PtyDriver):
    """Drive one subprocess attached to a Linux pseudo-terminal."""

    def __init__(self, config: PtyProcessConfig) -> None:
        super().__init__(config)
        self._master_fd: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._pid: int | None = None
        self._pgid: int | None = None
        self._started = False
        self._closed = False
        self._stream_eof = False
        self._eof = False
        self._exit_code: int | None = None
        self._read_error: OSError | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._drain_done = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the configured process on a Linux PTY."""
        if not sys.platform.startswith("linux"):
            raise RuntimeError(
                t(
                    ERR_TERMINAL_PTY_PLATFORM_MISMATCH,
                    driver="LinuxPtyDriver",
                    platform=sys.platform,
                )
            )

        async with self._start_lock:
            if self._started:
                raise RuntimeError(t(ERR_TERMINAL_PTY_ALREADY_STARTED))
            self._started = True

            master_fd, slave_fd = pty.openpty()
            process: subprocess.Popen[bytes] | None = None
            try:
                self._set_winsize(slave_fd, self.config.columns, self.config.rows)
                os.set_blocking(master_fd, False)
                process = subprocess.Popen(
                    self.config.argv,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=self.config.cwd,
                    env=dict(self.config.env),
                    start_new_session=True,
                    close_fds=True,
                )
                self._master_fd = master_fd
                master_fd = -1
                self._process = process
                self._pid = process.pid
                try:
                    self._pgid = os.getpgid(process.pid)
                except ProcessLookupError:
                    self._pgid = process.pid
                self._drain_task = asyncio.get_running_loop().create_task(self._drain_output())
            except BaseException:
                if process is not None:
                    self._signal_process_group(
                        process.pid,
                        signal_module.SIGKILL,
                    )
                    await asyncio.to_thread(process.wait)
                if self._drain_task is not None:
                    self._drain_task.cancel()
                    self._drain_task = None
                self._close_master_fd()
                if master_fd >= 0:
                    os.close(master_fd)
                raise
            finally:
                os.close(slave_fd)

    async def write(self, data: str) -> int:
        """Write UTF-8 encoded text to the PTY master."""
        if not isinstance(data, str):
            raise TypeError(t(ERR_TERMINAL_PTY_INPUT_INVALID, field="data"))
        if not data:
            raise ValueError(t(ERR_TERMINAL_PTY_INPUT_INVALID, field="data"))
        self._require_started()

        async with self._write_lock:
            if self._stream_eof or self._eof or self._closed:
                raise RuntimeError(t(ERR_TERMINAL_PTY_CLOSED))
            payload = data.encode("utf-8")
            written = 0
            while written < len(payload):
                master_fd = self._master_fd
                if master_fd is None:
                    raise RuntimeError(t(ERR_TERMINAL_PTY_CLOSED))
                try:
                    count = os.write(master_fd, payload[written:])
                except BlockingIOError:
                    await self._wait_for_writable(master_fd)
                    continue
                if count <= 0:
                    raise OSError(t(ERR_TERMINAL_PTY_WRITE_STALLED))
                written += count
            return written

    async def resize(self, columns: int, rows: int) -> None:
        """Set the PTY window size."""
        if not isinstance(columns, int) or isinstance(columns, bool) or not 1 <= columns <= 1_000:
            raise ValueError(t(ERR_TERMINAL_PTY_DIMENSIONS_INVALID))
        if not isinstance(rows, int) or isinstance(rows, bool) or not 1 <= rows <= 1_000:
            raise ValueError(t(ERR_TERMINAL_PTY_DIMENSIONS_INVALID))
        self._require_started()
        if self._stream_eof or self._eof or self._closed:
            raise RuntimeError(t(ERR_TERMINAL_PTY_CLOSED))
        master_fd = self._master_fd
        if master_fd is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_CLOSED))
        self._set_winsize(master_fd, columns, rows)

    async def send_signal(self, signal: TerminalSignal) -> None:
        """Send a signal to the complete PTY process group."""
        try:
            terminal_signal = TerminalSignal(signal)
        except (TypeError, ValueError) as exc:
            raise ValueError(t(ERR_TERMINAL_PTY_SIGNAL_INVALID, signal=signal)) from exc

        self._require_started()
        if self._closed or self._eof:
            return

        signal_number = {
            TerminalSignal.INTERRUPT: signal_module.SIGINT,
            TerminalSignal.TERMINATE: signal_module.SIGTERM,
            TerminalSignal.KILL: signal_module.SIGKILL,
        }[terminal_signal]
        self._signal_process_group(self._pgid, signal_number)

    async def wait(self) -> int:
        """Wait for output drain completion and return the root exit code."""
        self._require_started()
        drain_task = self._drain_task
        if drain_task is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_DRAIN_UNAVAILABLE))
        await drain_task
        if self._read_error is not None:
            raise self._read_error
        if not self._eof or self._exit_code is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_STATE_INVALID))
        return self._exit_code

    async def close(self, force: bool = False) -> None:
        """Terminate the process group and release PTY resources."""
        acquire_task = asyncio.create_task(self._close_lock.acquire())
        cancellation_requested = False
        while True:
            try:
                await asyncio.shield(acquire_task)
                break
            except asyncio.CancelledError:
                cancellation_requested = True

        try:
            if not self._started or self._closed:
                if cancellation_requested:
                    raise asyncio.CancelledError
                return
            if cancellation_requested:
                await self._run_forced_cleanup()
                self._closed = True
                raise asyncio.CancelledError
            try:
                await self._close_impl(force)
            except asyncio.CancelledError:
                await self._run_forced_cleanup()
                self._closed = True
                raise
            except BaseException:
                await self._run_forced_cleanup()
                self._closed = True
                raise
            self._closed = True
        finally:
            self._close_lock.release()

    @property
    def pid(self) -> int | None:
        """Return the root process identifier."""
        return self._pid

    @property
    def running(self) -> bool:
        """Return whether the root process is live before final EOF."""
        process = self._process
        return bool(self._started and process is not None and not self._eof and process.poll() is None)

    @property
    def eof(self) -> bool:
        """Return whether PTY EOF and root process completion are finalized."""
        return self._eof

    @property
    def exit_code(self) -> int | None:
        """Return the root process exit code, if collected."""
        return self._exit_code

    async def _drain_output(self) -> None:
        master_fd = self._master_fd
        if master_fd is None:
            self._drain_done.set()
            return

        loop = asyncio.get_running_loop()
        readable = loop.create_future()
        reader_added = False

        def on_readable() -> None:
            if readable.done():
                return
            try:
                chunk = os.read(master_fd, self.config.read_chunk_bytes)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno == errno.EIO:
                    self._stream_eof = True
                else:
                    self._read_error = exc
                readable.set_result(None)
                return

            if chunk == b"":
                self._stream_eof = True
                readable.set_result(None)
                return
            self._output_buffer.append(chunk)

        try:
            loop.add_reader(master_fd, on_readable)
            reader_added = True
            await readable
            if self._read_error is None and self._stream_eof:
                await self._finish_after_eof()
        finally:
            if reader_added:
                loop.remove_reader(master_fd)
            self._drain_done.set()

    async def _finish_after_eof(self) -> None:
        process = self._process
        if process is None:
            return
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                self._exit_code = exit_code
                self._eof = True
                return
            await asyncio.sleep(0.01)

    async def _close_impl(self, force: bool) -> None:
        if not self._eof:
            if force:
                self._signal_process_group(self._pgid, signal_module.SIGKILL)
            else:
                self._signal_process_group(self._pgid, signal_module.SIGTERM)
                if self.config.close_grace_seconds > 0:
                    await self._wait_for_drain(self.config.close_grace_seconds)
                if not self._eof:
                    self._signal_process_group(self._pgid, signal_module.SIGKILL)

        await self._wait_for_process_exit()
        await self._wait_for_drain_completion()
        self._close_master_fd()

    async def _run_forced_cleanup(self) -> None:
        cleanup_task = asyncio.create_task(self._force_cleanup())
        while True:
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                continue
            return

    async def _force_cleanup(self) -> None:
        if not self._eof:
            self._signal_process_group(self._pgid, signal_module.SIGKILL)
        await self._wait_for_process_exit()
        try:
            await self._wait_for_drain_completion()
        finally:
            self._close_master_fd()

    async def _wait_for_drain(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(
                asyncio.shield(self._drain_done.wait()),
                timeout,
            )
        except TimeoutError:
            return False
        return True

    async def _wait_for_drain_completion(self) -> None:
        drain_task = self._drain_task
        if drain_task is not None:
            await drain_task

    async def _wait_for_process_exit(self) -> None:
        process = self._process
        if process is None:
            return
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                if self._exit_code is None:
                    self._exit_code = exit_code
                return
            await asyncio.sleep(0.01)

    async def _wait_for_writable(self, master_fd: int) -> None:
        loop = asyncio.get_running_loop()
        writable = loop.create_future()

        def on_writable() -> None:
            if not writable.done():
                writable.set_result(None)

        loop.add_writer(master_fd, on_writable)
        try:
            await writable
        finally:
            loop.remove_writer(master_fd)

    def _require_started(self) -> None:
        if not self._started or self._process is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_NOT_STARTED))

    @staticmethod
    def _set_winsize(fd: int, columns: int, rows: int) -> None:
        fcntl.ioctl(
            fd,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, columns, 0, 0),
        )

    @staticmethod
    def _signal_process_group(pgid: int | None, signal_number: int) -> None:
        if pgid is None:
            return
        try:
            os.killpg(pgid, signal_number)
        except ProcessLookupError:
            return

    def _close_master_fd(self) -> None:
        master_fd = self._master_fd
        self._master_fd = None
        if master_fd is None:
            return
        try:
            os.close(master_fd)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise


__all__ = ["LinuxPtyDriver"]
