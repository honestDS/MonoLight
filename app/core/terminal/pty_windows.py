"""Windows ConPTY driver implementation."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from typing import Any

import psutil

if sys.platform == "win32":
    from winpty import PTY, Backend, WinptyError

from app.core.constants import (
    ERR_TERMINAL_CONPTY_PID_INVALID,
    ERR_TERMINAL_CONPTY_SPAWN_FAILED,
    ERR_TERMINAL_PROCESS_ACTION_INVALID,
    ERR_TERMINAL_PTY_ALREADY_STARTED,
    ERR_TERMINAL_PTY_CLOSED,
    ERR_TERMINAL_PTY_DIMENSIONS_INVALID,
    ERR_TERMINAL_PTY_EXECUTABLE_NOT_FOUND,
    ERR_TERMINAL_PTY_EXIT_CODE_MISSING,
    ERR_TERMINAL_PTY_INPUT_INVALID,
    ERR_TERMINAL_PTY_NOT_STARTED,
    ERR_TERMINAL_PTY_PLATFORM_MISMATCH,
    ERR_TERMINAL_PTY_SIGNAL_INVALID,
)
from app.core.i18n import t
from app.core.terminal.pty_base import PtyDriver, PtyProcessConfig
from app.core.terminal.schemas import TerminalSignal

_MAX_TERMINAL_DIMENSION = 1_000
_TERMINATE_WAIT_SECONDS = 0.2
_CLOSE_STATUS_POLL_INTERVAL_SECONDS = 0.02


class WindowsPtyDriver(PtyDriver):
    """Drive a process through a Windows ConPTY instance."""

    def __init__(self, config: PtyProcessConfig) -> None:
        super().__init__(config)
        self._pty: Any | None = None
        self._pid: int | None = None
        self._root_create_time: float | None = None
        self._known_processes: dict[int, float] = {}
        self._started = False
        self._alive = False
        self._eof = False
        self._exit_code: int | None = None
        self._drain_exception: BaseException | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._completion_event = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closing = False
        self._cleanup_complete = False

    async def start(self) -> None:
        """Start the configured command in a ConPTY process."""
        async with self._start_lock:
            if self._started:
                raise RuntimeError(t(ERR_TERMINAL_PTY_ALREADY_STARTED))
            self._started = True
            if sys.platform != "win32":
                raise RuntimeError(
                    t(
                        ERR_TERMINAL_PTY_PLATFORM_MISMATCH,
                        driver="WindowsPtyDriver",
                        platform=sys.platform,
                    )
                )

            executable = await asyncio.to_thread(
                shutil.which,
                self.config.argv[0],
                path=self.config.env.get("PATH"),
            )
            if executable is None:
                raise FileNotFoundError(
                    t(
                        ERR_TERMINAL_PTY_EXECUTABLE_NOT_FOUND,
                        executable=self.config.argv[0],
                    )
                )

            arguments = subprocess.list2cmdline(self.config.argv[1:])
            environment = "\0".join(f"{key}={value}" for key, value in self.config.env.items()) + "\0"

            pty = await asyncio.to_thread(
                PTY,
                self.config.columns,
                self.config.rows,
                backend=Backend.ConPTY,
            )
            self._pty = pty
            try:
                spawned = await asyncio.to_thread(
                    pty.spawn,
                    executable,
                    cmdline=f" {arguments}" if arguments else None,
                    cwd=self.config.cwd,
                    env=environment,
                )
                if spawned is not True:
                    raise RuntimeError(t(ERR_TERMINAL_CONPTY_SPAWN_FAILED))
                spawned_pid = pty.pid
                if not isinstance(spawned_pid, int) or isinstance(spawned_pid, bool) or spawned_pid <= 0:
                    raise RuntimeError(t(ERR_TERMINAL_CONPTY_PID_INVALID))
                self._pid = spawned_pid
                process = psutil.Process(self._pid)
                self._root_create_time = await asyncio.to_thread(
                    process.create_time,
                )
                self._known_processes[self._pid] = self._root_create_time
            except BaseException:
                await self._discard_pty_after_start_failure(pty)
                raise

            self._alive = True
            self._drain_task = asyncio.create_task(self._drain_output())

    async def write(self, data: str) -> int:
        """Write non-empty text to the ConPTY input stream."""
        if not isinstance(data, str):
            raise TypeError(t(ERR_TERMINAL_PTY_INPUT_INVALID, field="data"))
        if not data:
            raise ValueError(t(ERR_TERMINAL_PTY_INPUT_INVALID, field="data"))
        if not self._started or self._pty is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_NOT_STARTED))
        if self._eof or self._completion_event.is_set():
            raise RuntimeError(t(ERR_TERMINAL_PTY_CLOSED))

        input_byte_length = len(data.encode("utf-8"))
        await asyncio.to_thread(self._pty.write, data)
        return input_byte_length

    async def resize(self, columns: int, rows: int) -> None:
        """Set the ConPTY dimensions."""
        if not self._valid_dimension(columns) or not self._valid_dimension(rows):
            raise ValueError(t(ERR_TERMINAL_PTY_DIMENSIONS_INVALID))
        if not self._started or self._pty is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_NOT_STARTED))
        if self._eof or self._completion_event.is_set():
            raise RuntimeError(t(ERR_TERMINAL_PTY_CLOSED))

        await asyncio.to_thread(self._pty.set_size, columns, rows)

    async def send_signal(self, signal: TerminalSignal) -> None:
        """Send a terminal interrupt or a signal to the process tree."""
        if not self._started:
            raise RuntimeError(t(ERR_TERMINAL_PTY_NOT_STARTED))
        if self._completion_event.is_set() or self._eof or self._cleanup_complete:
            return

        if signal is TerminalSignal.INTERRUPT:
            await self.write("\x03")
            return
        if signal is TerminalSignal.TERMINATE:
            await self._signal_process_tree("terminate")
            return
        if signal is TerminalSignal.KILL:
            await self._signal_process_tree("kill")
            return
        raise ValueError(t(ERR_TERMINAL_PTY_SIGNAL_INVALID, signal=signal))

    async def wait(self) -> int:
        """Wait for output draining to finish and return the exit code."""
        if not self._started or self._drain_task is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_NOT_STARTED))

        await self._completion_event.wait()
        await self._drain_task
        if self._drain_exception is not None:
            raise self._drain_exception
        if self._exit_code is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_EXIT_CODE_MISSING))
        return self._exit_code

    async def close(self, force: bool = False) -> None:
        """Close the PTY and ensure that its process tree is terminated."""
        async with self._close_lock:
            if not self._started or self._cleanup_complete:
                return
            try:
                await self._close_impl(force)
            except asyncio.CancelledError:
                await self._complete_cancelled_close()
                raise

    @property
    def pid(self) -> int | None:
        """Return the root process identifier."""
        return self._pid

    @property
    def running(self) -> bool:
        """Return a non-blocking process liveness result."""
        if not self._started or self._completion_event.is_set() or self._pty is None:
            return False
        try:
            self._alive = bool(self._pty.isalive())
        except Exception:
            return self._alive
        return self._alive

    @property
    def eof(self) -> bool:
        """Return whether the output stream reached EOF."""
        return self._eof

    @property
    def exit_code(self) -> int | None:
        """Return the normalized process exit code."""
        return self._exit_code

    async def _drain_output(self) -> None:
        pty = self._pty
        if pty is None:
            return

        try:
            while True:
                await self._remember_process_tree()
                data = await asyncio.to_thread(pty.read, blocking=True)
                if data:
                    self._output_buffer.append(
                        data.encode("utf-8", errors="replace"),
                    )

                is_eof = await asyncio.to_thread(pty.iseof)
                if is_eof:
                    break
                self._alive = bool(await asyncio.to_thread(pty.isalive))
                if not self._alive:
                    continue
        except WinptyError as error:
            if not await self._confirmed_end(pty):
                self._record_drain_exception(error)
                return
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._record_drain_exception(error)
            return

        await self._finish_drain(pty)

    async def _confirmed_end(self, pty: Any) -> bool:
        is_eof = False
        try:
            is_eof = bool(await asyncio.to_thread(pty.iseof))
        except WinptyError:
            pass

        try:
            is_alive = bool(await asyncio.to_thread(pty.isalive))
        except WinptyError:
            is_alive = False
        self._alive = is_alive
        return is_eof or not is_alive

    async def _finish_drain(self, pty: Any) -> None:
        try:
            status = await asyncio.to_thread(pty.get_exitstatus)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            self._record_drain_exception(error)
            return

        self._alive = False
        self._eof = True
        self._exit_code = self._normalize_exit_code(status)
        self._completion_event.set()

    def _record_drain_exception(self, error: BaseException) -> None:
        self._drain_exception = error
        self._alive = False
        if not self._closing:
            self._completion_event.set()

    async def _close_impl(self, force: bool) -> None:
        self._closing = True
        try:
            if force:
                await self._signal_process_tree("kill")
            elif not self._completion_event.is_set():
                try:
                    await self.write("\x03")
                except Exception:
                    pass
                await self._wait_for_completion(self.config.close_grace_seconds)
                if not self._completion_event.is_set():
                    await self._signal_process_tree("terminate")
                await asyncio.sleep(_TERMINATE_WAIT_SECONDS)
                await self._signal_process_tree("kill")

            await self._cancel_io()
            await self._wait_for_drain()
            await self._signal_process_tree("kill")
            await self._converge_close_state()
            if not self._completion_event.is_set():
                self._completion_event.set()
            await self._release_pty()
        finally:
            self._closing = False

    async def _converge_close_state(self) -> None:
        if self._exit_code is not None or self._pty is None:
            return

        pty = self._pty
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(
            _TERMINATE_WAIT_SECONDS,
            self.config.close_grace_seconds,
        )
        while True:
            try:
                is_alive = bool(await asyncio.to_thread(pty.isalive))
            except asyncio.CancelledError:
                raise
            except BaseException:
                is_alive = None

            if is_alive is False:
                self._alive = False
                try:
                    status = await asyncio.to_thread(pty.get_exitstatus)
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    status = None
                else:
                    self._eof = True
                    self._completion_event.set()
                    self._exit_code = self._normalize_exit_code(status)
                    if self._exit_code is not None:
                        self._drain_exception = None
                        return
                    return
            elif is_alive is True:
                self._alive = True

            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(
                min(_CLOSE_STATUS_POLL_INTERVAL_SECONDS, remaining),
            )

    async def _complete_cancelled_close(self) -> None:
        cleanup_task = asyncio.create_task(self._force_cleanup())
        while True:
            try:
                await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError:
                continue

    async def _force_cleanup(self) -> None:
        try:
            await self._signal_process_tree("kill")
        except BaseException:
            pass
        try:
            await self._cancel_io()
        except BaseException:
            pass
        try:
            await self._wait_for_drain()
        except BaseException:
            pass
        try:
            await self._release_pty()
        except BaseException:
            self._pty = None
            self._cleanup_complete = True

    async def _wait_for_completion(self, timeout: float) -> None:
        if timeout <= 0 or self._completion_event.is_set():
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(self._completion_event.wait()),
                timeout,
            )
        except TimeoutError:
            return

    async def _cancel_io(self) -> None:
        pty = self._pty
        if pty is None:
            return
        try:
            await asyncio.to_thread(pty.cancel_io)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise

    async def _wait_for_drain(self) -> None:
        task = self._drain_task
        if task is None:
            return
        try:
            await task
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                return

    async def _release_pty(self) -> None:
        await self._cancel_io()
        self._pty = None
        self._alive = False
        self._cleanup_complete = True

    async def _signal_process_tree(self, action: str) -> None:
        if self._pid is None or self._root_create_time is None:
            return
        snapshot = await asyncio.to_thread(
            self._signal_process_tree_sync,
            self._pid,
            self._root_create_time,
            action,
            dict(self._known_processes),
        )
        self._known_processes.update(snapshot)

    @staticmethod
    def _signal_process_tree_sync(
        pid: int,
        root_create_time: float,
        action: str,
        known_processes: dict[int, float] | None = None,
    ) -> dict[int, float]:
        known_processes = known_processes or {}
        try:
            root = psutil.Process(pid)
            if root.create_time() != root_create_time:
                return {}
        except (psutil.AccessDenied, psutil.ZombieProcess):
            return {}
        except psutil.NoSuchProcess:
            processes: list[tuple[int, psutil.Process]] = []
            for known_pid, known_create_time in known_processes.items():
                if known_pid == pid:
                    continue
                try:
                    process = psutil.Process(known_pid)
                    if process.create_time() == known_create_time:
                        processes.append((0, process))
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    continue
            WindowsPtyDriver._apply_process_action(processes, action)
            return {}

        processes: list[tuple[int, psutil.Process]] = [(0, root)]
        pending: list[tuple[int, psutil.Process]] = [(0, root)]
        seen = {pid}
        while pending:
            depth, process = pending.pop()
            try:
                children = process.children()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            for child in children:
                if child.pid in seen:
                    continue
                seen.add(child.pid)
                child_depth = depth + 1
                processes.append((child_depth, child))
                pending.append((child_depth, child))

        processes.sort(key=lambda item: item[0], reverse=True)
        snapshot: dict[int, float] = {}
        for _, process in processes:
            try:
                snapshot[process.pid] = process.create_time()
            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
            ):
                continue
        WindowsPtyDriver._apply_process_action(processes, action)
        return snapshot

    @staticmethod
    def _apply_process_action(
        processes: list[tuple[int, psutil.Process]],
        action: str,
    ) -> None:
        for _, process in processes:
            try:
                if action == "terminate":
                    process.terminate()
                elif action == "kill":
                    process.kill()
                else:
                    raise ValueError(t(ERR_TERMINAL_PROCESS_ACTION_INVALID, action=action))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

    async def _remember_process_tree(self) -> None:
        if self._pid is None or self._root_create_time is None:
            return
        snapshot = await asyncio.to_thread(
            self._collect_process_tree_sync,
            self._pid,
            self._root_create_time,
        )
        self._known_processes.update(snapshot)

    @staticmethod
    def _collect_process_tree_sync(
        pid: int,
        root_create_time: float,
    ) -> dict[int, float]:
        try:
            root = psutil.Process(pid)
            if root.create_time() != root_create_time:
                return {}
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return {}

        processes: list[psutil.Process] = [root]
        pending = [root]
        seen = {pid}
        while pending:
            process = pending.pop()
            try:
                children = process.children()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            for child in children:
                if child.pid in seen:
                    continue
                seen.add(child.pid)
                processes.append(child)
                pending.append(child)

        snapshot: dict[int, float] = {}
        for process in processes:
            try:
                snapshot[process.pid] = process.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return snapshot

    async def _discard_pty_after_start_failure(self, pty: Any) -> None:
        try:
            await asyncio.to_thread(pty.cancel_io)
        except BaseException:
            pass
        self._pty = None
        pty = None

    @staticmethod
    def _valid_dimension(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_TERMINAL_DIMENSION

    @staticmethod
    def _normalize_exit_code(status: int | None) -> int | None:
        if status is None:
            return None
        code = int(status)
        if code > 0x7FFFFFFF:
            return code - 0x100000000
        return code


__all__ = ["WindowsPtyDriver"]
