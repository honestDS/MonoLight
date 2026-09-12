import asyncio
from typing import Any

from app.core.constants import (
    ERR_TERMINAL_PROCESS_ACTION_INVALID,
    ERR_TERMINAL_PTY_CLOSED,
    ERR_TERMINAL_PTY_EXIT_CODE_MISSING,
    ERR_TERMINAL_PTY_NOT_STARTED,
    ERR_TERMINAL_PTY_STATE_INVALID,
)
from app.core.crud.terminal.session import (
    terminal_control_command_crud,
)
from app.core.i18n import t
from app.core.terminal.schemas import (
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalAction,
    TerminalOutputReadStatus,
    TerminalReadResult,
    TerminalSessionStatus,
)
from app.models.terminal_session import TerminalControlCommand
from app.providers.database import AsyncSessionLocal

from .manager_common import (
    logger,
)
from .session_runtime_common import (
    _read_without_driver_result,
    _TerminalLeaseLost,
)

__all__ = [
    "TerminalSessionRuntimeCommands",
]


class TerminalSessionRuntimeCommands:
    async def _process_command(self, command: TerminalControlCommand) -> None:
        if command.id is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_STATE_INVALID))

        try:
            result = await self._execute_command(command)
        except asyncio.CancelledError:
            raise
        except _TerminalLeaseLost:
            raise
        except Exception as exc:
            async with AsyncSessionLocal() as db:
                marked = await terminal_control_command_crud.mark_failed(
                    db,
                    command.id,
                    self.worker_id,
                    error=str(exc),
                )
            if not marked:
                self._lease_lost_confirmed = True
                logger.error(
                    "Terminal control command lease lost while marking failure",
                    extra={"terminal_session_id": self.terminal_session_id, "command_id": command.id},
                )
                raise _TerminalLeaseLost
            return

        async with AsyncSessionLocal() as db:
            marked = await terminal_control_command_crud.mark_succeeded(
                db,
                command.id,
                self.worker_id,
                result,
            )
        if not marked:
            self._lease_lost_confirmed = True
            logger.error(
                "Terminal control command lease lost while marking success",
                extra={"terminal_session_id": self.terminal_session_id, "command_id": command.id},
            )
            raise _TerminalLeaseLost

    async def _execute_command(self, command: TerminalControlCommand) -> dict[str, Any]:
        try:
            action = TerminalAction(command.action)
        except (TypeError, ValueError) as exc:
            raise ValueError(t(ERR_TERMINAL_PROCESS_ACTION_INVALID, action=command.action)) from exc

        driver = self._driver
        payload = command.payload
        if driver is None:
            if action is TerminalAction.READ:
                return self._read_without_driver(payload)
            if action is TerminalAction.CLOSE:
                return self._close_result()
            if action in {
                TerminalAction.WRITE,
                TerminalAction.RESIZE,
            }:
                error_key = ERR_TERMINAL_PTY_CLOSED if self._status in TERMINAL_SESSION_FINAL_STATUSES else ERR_TERMINAL_PTY_NOT_STARTED
                raise RuntimeError(t(error_key))
            raise RuntimeError(t(ERR_TERMINAL_PROCESS_ACTION_INVALID, action=command.action))

        if action is TerminalAction.READ:
            read = driver.read_output(payload["offset"], payload["max_bytes"])
            if read.data:
                read_status = TerminalOutputReadStatus.TRUNCATED if read.truncated else TerminalOutputReadStatus.OK
            else:
                read_status = TerminalOutputReadStatus.EMPTY
            return TerminalReadResult(
                terminal_session_id=self.terminal_session_id,
                read_status=read_status,
                requested_offset=read.requested_offset,
                start_offset=read.start_offset,
                next_offset=read.next_offset,
                oldest_available_offset=read.oldest_available_offset,
                latest_offset=read.latest_offset,
                sequence=read.sequence,
                output=read.data.decode("utf-8", errors="replace"),
                eof=read.eof or self._status in TERMINAL_SESSION_FINAL_STATUSES,
            ).model_dump(mode="json")

        if action is TerminalAction.WRITE:
            output_offset_before_write = driver.resource_snapshot().output_buffer.next_offset
            written = await driver.write(payload["data"])
            return {
                "bytes_written": written,
                "output_offset_before_write": output_offset_before_write,
            }

        if action is TerminalAction.RESIZE:
            columns = payload["columns"]
            rows = payload["rows"]
            await driver.resize(columns, rows)
            return {"columns": columns, "rows": rows}

        if action is TerminalAction.CLOSE:
            return await self._execute_close(bool(payload["force"]))

        raise RuntimeError(t(ERR_TERMINAL_PROCESS_ACTION_INVALID, action=command.action))

    def _read_without_driver(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _read_without_driver_result(self.terminal_session_id, self._get_output_buffer(), payload)

    async def _execute_close(self, force: bool) -> dict[str, Any]:
        driver = self._driver
        if driver is None:
            raise RuntimeError(t(ERR_TERMINAL_PTY_NOT_STARTED))

        was_final = self._status in TERMINAL_SESSION_FINAL_STATUSES
        if not was_final:
            await self._update_runtime_snapshot(
                TerminalSessionStatus.CLOSING,
                output_buffer=self._get_output_buffer(),
            )

        try:
            await driver.close(force=force)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await driver.close(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Terminal session close cleanup failed",
                    extra={"terminal_session_id": self.terminal_session_id},
                )
            if not was_final:
                await self._update_runtime_snapshot(
                    TerminalSessionStatus.FAILED,
                    output_buffer=self._get_output_buffer(),
                    failure_reason=str(exc),
                )
            raise

        if was_final:
            return self._close_result()

        wait_task = self._wait_task
        exit_code: int | None = None
        try:
            if wait_task is not None:
                exit_code = await wait_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._update_runtime_snapshot(
                TerminalSessionStatus.FAILED,
                output_buffer=self._get_output_buffer(),
                failure_reason=str(exc),
            )
            raise

        snapshot = driver.resource_snapshot()
        if exit_code is None:
            exit_code = snapshot.exit_code
        if exit_code is None:
            failure_reason = t(ERR_TERMINAL_PTY_EXIT_CODE_MISSING)
            await self._update_runtime_snapshot(
                TerminalSessionStatus.FAILED,
                output_buffer=snapshot.output_buffer,
                failure_reason=failure_reason,
            )
            raise RuntimeError(failure_reason)

        await self._update_runtime_snapshot(
            TerminalSessionStatus.EXITED,
            output_buffer=snapshot.output_buffer,
            exit_code=exit_code,
        )
        return self._close_result()

    def _close_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self._status.value}
        if self._exit_code is not None:
            result["exit_code"] = self._exit_code
        return result
