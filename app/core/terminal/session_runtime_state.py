import asyncio

from app.core.constants import (
    ERR_TERMINAL_PTY_CLOSED,
    ERR_TERMINAL_WORKER_STOPPED,
)
from app.core.crud.terminal.session import (
    terminal_session_crud,
)
from app.core.i18n import t
from app.core.terminal.schemas import (
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalOutputBufferState,
    TerminalSessionStatus,
)
from app.providers.database import AsyncSessionLocal

from .audit_lifecycle import (
    _update_terminal_confirmation_status,
    finalize_terminal_session_audit,
)
from .manager_common import (
    logger,
)
from .session_runtime_common import (
    _TerminalLeaseLost,
)

__all__ = [
    "TerminalSessionRuntimeState",
]


class TerminalSessionRuntimeState:
    async def _update_runtime_snapshot(
        self,
        status: TerminalSessionStatus,
        *,
        output_buffer: TerminalOutputBufferState,
        exit_code: int | None = None,
        failure_reason: str | None = None,
    ) -> None:
        if self._status is status and status in TERMINAL_SESSION_FINAL_STATUSES:
            return

        audit_round_finished_id: int | None = None
        async with AsyncSessionLocal() as db:
            updated = await terminal_session_crud.update_runtime_snapshot(
                db,
                self.terminal_session_id,
                self.worker_id,
                status,
                output_buffer,
                exit_code=exit_code,
                failure_reason=failure_reason,
                process_identity=self._process_identity,
                commit=False,
            )
            if updated and status in TERMINAL_SESSION_FINAL_STATUSES:
                audit_round_finished_id = await finalize_terminal_session_audit(
                    db,
                    self.terminal_session,
                    status=status,
                    exit_code=exit_code,
                    failure_reason=failure_reason,
                )
            await db.commit()
        if not updated:
            self._lease_lost_confirmed = True
            logger.error(
                "Terminal session runtime lease lost",
                extra={"terminal_session_id": self.terminal_session_id},
            )
            raise _TerminalLeaseLost

        self._status = status
        if status is TerminalSessionStatus.EXITED:
            self._exit_code = exit_code
            self._failure_reason = None
        elif status is TerminalSessionStatus.FAILED:
            self._exit_code = None
            self._failure_reason = failure_reason
        else:
            self._exit_code = None
            self._failure_reason = None

        if audit_round_finished_id is not None:
            async with AsyncSessionLocal() as db:
                try:
                    await _update_terminal_confirmation_status(db, audit_record_id=audit_round_finished_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Terminal session confirmation status projection failed",
                        extra={
                            "terminal_session_id": self.terminal_session_id,
                            "audit_record_id": audit_round_finished_id,
                        },
                    )

    async def _finalize_after_run(self) -> None:
        if self._status not in TERMINAL_SESSION_FINAL_STATUSES and not self._lease_lost_confirmed:
            try:
                await self._update_runtime_snapshot(
                    TerminalSessionStatus.LOST,
                    output_buffer=self._get_output_buffer(),
                    failure_reason=self._stop_reason or t(ERR_TERMINAL_WORKER_STOPPED),
                )
            except _TerminalLeaseLost:
                self._lease_lost_confirmed = True
            except Exception:
                logger.exception(
                    "Terminal session lost-state persistence failed",
                    extra={"terminal_session_id": self.terminal_session_id},
                )
                return

        if self._lease_lost_confirmed:
            return

        command_failure_reason = self._stop_reason or (t(ERR_TERMINAL_PTY_CLOSED) if self._status in TERMINAL_SESSION_FINAL_STATUSES else t(ERR_TERMINAL_WORKER_STOPPED))
        async with AsyncSessionLocal() as db:
            await terminal_session_crud.fail_unfinished_commands(
                db,
                self.terminal_session_id,
                command_failure_reason,
                worker_id=self.worker_id,
                commit=False,
            )
            released = await terminal_session_crud.release_claim(
                db,
                self.terminal_session_id,
                self.worker_id,
                commit=False,
            )
            if released:
                await db.commit()
            else:
                await db.rollback()

    def _get_output_buffer(self) -> TerminalOutputBufferState:
        driver = self._driver
        if driver is None:
            return TerminalOutputBufferState(
                capacity_bytes=self.terminal_session.output_capacity_bytes,
                oldest_offset=self.terminal_session.oldest_output_offset,
                next_offset=self.terminal_session.next_output_offset,
                oldest_sequence=self.terminal_session.oldest_output_sequence,
                next_sequence=self.terminal_session.next_output_sequence,
            )
        return driver.resource_snapshot().output_buffer

    async def _await_wait_task(self) -> None:
        wait_task = self._wait_task
        if wait_task is None:
            return
        try:
            await wait_task
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
