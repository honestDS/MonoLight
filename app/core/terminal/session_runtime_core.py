import asyncio

from app.core.constants import (
    ERR_TERMINAL_PTY_EXIT_CODE_MISSING,
    ERR_TERMINAL_WORKER_STOPPED,
)
from app.core.crud.terminal.session import (
    terminal_control_command_crud,
)
from app.core.i18n import t
from app.core.terminal.process_config import build_interactive_shell_argv, build_subprocess_env
from app.core.terminal.pty_base import PtyDriver, PtyProcessConfig
from app.core.terminal.pty_factory import create_pty_driver
from app.core.terminal.recovery import capture_terminal_process_identity
from app.core.terminal.schemas import (
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalSessionStatus,
)
from app.models.terminal_session import TerminalControlCommand, TerminalSession
from app.providers.database import AsyncSessionLocal

from .manager_common import (
    TERMINAL_PROCESS_IDENTITY_REFRESH_INTERVAL_SECONDS,
    TERMINAL_SESSION_LEASE_SECONDS,
    TERMINAL_SESSION_POLL_INTERVAL_SECONDS,
    logger,
)
from .session_runtime_common import (
    _TerminalLeaseLost,
)

__all__ = [
    "TerminalSessionRuntimeCore",
]


class TerminalSessionRuntimeCore:
    def __init__(
        self,
        terminal_session: TerminalSession,
        worker_id: str,
    ) -> None:
        self.terminal_session = terminal_session
        self.terminal_session_id = terminal_session.terminal_session_id
        self.worker_id = worker_id
        self._stop_event = asyncio.Event()
        self._driver_start_finished = asyncio.Event()
        self._driver: PtyDriver | None = None
        self._wait_task: asyncio.Task[int] | None = None
        self._status = terminal_session.status
        self._exit_code = terminal_session.exit_code
        self._failure_reason = terminal_session.failure_reason
        self._process_identity = terminal_session.process_identity
        self._stop_reason: str | None = None
        self._lease_lost_confirmed = False
        self._last_process_identity_refresh_at = 0.0

    def request_stop(self, reason: str | None = None, *, lease_lost: bool = False) -> None:
        if reason is not None and (self._stop_reason is None or lease_lost):
            self._stop_reason = reason
        elif self._stop_reason is None:
            self._stop_reason = t(ERR_TERMINAL_WORKER_STOPPED)
        if lease_lost:
            self._lease_lost_confirmed = True
        self._stop_event.set()

    async def run(self) -> None:
        try:
            if await self._initialize():
                await self._serve()
        except _TerminalLeaseLost:
            self._lease_lost_confirmed = True
        except asyncio.CancelledError:
            self.request_stop(t(ERR_TERMINAL_WORKER_STOPPED))
            raise
        except Exception:
            self.request_stop(t(ERR_TERMINAL_WORKER_STOPPED))
            logger.exception(
                "Terminal session runtime failed",
                extra={"terminal_session_id": self.terminal_session_id},
            )
        finally:
            close_cancelled = False
            try:
                await self.force_close()
            except asyncio.CancelledError:
                close_cancelled = True
            except Exception:
                logger.exception(
                    "Terminal session driver force close failed",
                    extra={"terminal_session_id": self.terminal_session_id},
                )
            finally:
                try:
                    await self._await_wait_task()
                except asyncio.CancelledError:
                    close_cancelled = True
            await self._finalize_after_run()
            if close_cancelled:
                raise asyncio.CancelledError

    async def force_close(self) -> None:
        self._stop_event.set()
        await self._driver_start_finished.wait()
        driver = self._driver
        if driver is not None:
            await driver.close(force=True)

    async def _initialize(self) -> bool:
        try:
            config = PtyProcessConfig(
                argv=build_interactive_shell_argv(self.terminal_session.command),
                cwd=self.terminal_session.working_directory,
                env=build_subprocess_env(),
                output_capacity_bytes=self.terminal_session.output_capacity_bytes,
            )
            self._driver = create_pty_driver(config)
            await self._driver.start()
            process_identity = await capture_terminal_process_identity(self._driver.pid, self._process_identity) if self._driver.pid is not None else None
            if process_identity is not None:
                self._process_identity = process_identity
            self._last_process_identity_refresh_at = asyncio.get_running_loop().time()
            self._wait_task = asyncio.create_task(self._driver.wait())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Terminal session startup failed",
                extra={"terminal_session_id": self.terminal_session_id},
            )
            driver = self._driver
            if driver is not None:
                try:
                    await driver.close(force=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Terminal session startup cleanup failed",
                        extra={"terminal_session_id": self.terminal_session_id},
                    )
                finally:
                    self._driver = None
            try:
                await self._update_runtime_snapshot(
                    TerminalSessionStatus.FAILED,
                    output_buffer=self._get_output_buffer(),
                    failure_reason=str(exc),
                )
            except _TerminalLeaseLost:
                return False
            return True
        finally:
            self._driver_start_finished.set()

        if self._stop_event.is_set():
            return False
        try:
            await self._update_runtime_snapshot(
                TerminalSessionStatus.RUNNING,
                output_buffer=self._get_output_buffer(),
            )
        except _TerminalLeaseLost:
            return False
        return True

    async def _serve(self) -> None:
        while not self._stop_event.is_set():
            driver = self._driver
            if driver is None:
                if self._status not in TERMINAL_SESSION_FINAL_STATUSES:
                    await self._update_runtime_snapshot(
                        self._status,
                        output_buffer=self._get_output_buffer(),
                        exit_code=self._exit_code,
                        failure_reason=self._failure_reason,
                    )
            else:
                snapshot = driver.resource_snapshot()
                wait_task_done = self._wait_task is not None and self._wait_task.done()
                if wait_task_done and self._status not in TERMINAL_SESSION_FINAL_STATUSES:
                    await self._complete_natural_exit()
                elif self._status not in TERMINAL_SESSION_FINAL_STATUSES:
                    await self._refresh_process_identity()
                    snapshot = driver.resource_snapshot()
                    await self._update_runtime_snapshot(
                        self._status,
                        output_buffer=snapshot.output_buffer,
                        exit_code=self._exit_code,
                        failure_reason=self._failure_reason,
                    )

            if self._stop_event.is_set():
                break
            command = await self._claim_next_command()
            if command is not None:
                await self._process_command(command)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=TERMINAL_SESSION_POLL_INTERVAL_SECONDS,
                )
            except TimeoutError:
                pass

    async def _complete_natural_exit(self) -> None:
        driver = self._driver
        wait_task = self._wait_task
        if driver is None or wait_task is None:
            return

        exit_code: int | None = None
        failure_reason: str | None = None
        try:
            exit_code = wait_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_reason = str(exc)

        try:
            await driver.close(force=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if failure_reason is None:
                failure_reason = str(exc)

        snapshot = driver.resource_snapshot()
        if failure_reason is not None:
            await self._update_runtime_snapshot(
                TerminalSessionStatus.FAILED,
                output_buffer=snapshot.output_buffer,
                failure_reason=failure_reason,
            )
            return

        if exit_code is None:
            exit_code = snapshot.exit_code
        if exit_code is None:
            failure_reason = t(ERR_TERMINAL_PTY_EXIT_CODE_MISSING)
            await self._update_runtime_snapshot(
                TerminalSessionStatus.FAILED,
                output_buffer=snapshot.output_buffer,
                failure_reason=failure_reason,
            )
            return

        await self._update_runtime_snapshot(
            TerminalSessionStatus.EXITED,
            output_buffer=snapshot.output_buffer,
            exit_code=exit_code,
        )

    async def _claim_next_command(self) -> TerminalControlCommand | None:
        async with AsyncSessionLocal() as db:
            return await terminal_control_command_crud.claim_next(
                db,
                self.terminal_session_id,
                self.worker_id,
                TERMINAL_SESSION_LEASE_SECONDS,
            )

    async def _refresh_process_identity(self) -> None:
        driver = self._driver
        if driver is None or driver.pid is None:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if now - self._last_process_identity_refresh_at < TERMINAL_PROCESS_IDENTITY_REFRESH_INTERVAL_SECONDS:
            return
        process_identity = await capture_terminal_process_identity(driver.pid, self._process_identity)
        self._last_process_identity_refresh_at = now
        if process_identity is not None:
            self._process_identity = process_identity
