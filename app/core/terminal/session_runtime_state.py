import asyncio
from typing import Any

from app.core.constants import (
    ERR_TERMINAL_PTY_CLOSED,
    ERR_TERMINAL_WORKER_STOPPED,
    TERMINAL_WATCH_MAX_OUTPUT_BYTES,
)
from app.core.crud.task.background import background_task_crud
from app.core.crud.terminal.session import (
    terminal_control_command_crud,
    terminal_session_crud,
)
from app.core.i18n import t
from app.core.terminal.schemas import (
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalAction,
    TerminalOutputBufferState,
    TerminalSessionStatus,
)
from app.models.background_task import BackgroundTaskStatus
from app.models.terminal_session import TerminalControlCommandStatus
from app.providers.database import AsyncSessionLocal

from .audit_lifecycle import (
    _update_terminal_confirmation_status,
    cleanup_terminal_session,
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
        elif status in {TerminalSessionStatus.FAILED, TerminalSessionStatus.LOST}:
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

    async def _finalize_after_run(self, *, process_closed: bool = True) -> None:
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

        if not process_closed or self._status is TerminalSessionStatus.LOST:
            command_failure_reason = self._failure_reason or self._stop_reason or t(ERR_TERMINAL_WORKER_STOPPED)
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
            return

        await self._persist_completion_and_cleanup()

    async def _persist_completion_and_cleanup(self) -> None:
        if self._lease_lost_confirmed or self._status not in TERMINAL_SESSION_FINAL_STATUSES:
            return

        from app.core.background_tasks.terminal_watch_coordinator import persist_terminal_completion
        from app.core.terminal.manager import TerminalSessionManager

        async with AsyncSessionLocal() as db:
            terminal_session = await terminal_session_crud.lock_session(
                db,
                self.terminal_session_id,
                worker_id=self.worker_id,
            )
            if terminal_session is None:
                return

            commands = await terminal_control_command_crud.list_by_session(
                db,
                self.terminal_session_id,
            )
            watches = await background_task_crud.list_terminal_watches(
                db,
                uid=terminal_session.uid,
                session_id=terminal_session.session_id,
                terminal_session_id=self.terminal_session_id,
            )
            active_statuses = {
                BackgroundTaskStatus.PENDING,
                BackgroundTaskStatus.RUNNING,
            }
            active_auto_watch = next(
                (watch for watch in reversed(watches) if watch.status in active_statuses and isinstance(watch.extra, dict) and watch.extra.get("auto_terminal_watch") is True),
                None,
            )
            auto_arguments = active_auto_watch.arguments if active_auto_watch is not None and isinstance(active_auto_watch.arguments, dict) else {}
            after_offset = auto_arguments.get("after_offset", 0)
            if not isinstance(after_offset, int) or isinstance(after_offset, bool) or after_offset < 0:
                after_offset = 0

            output_bytes = b""
            start_offset = after_offset
            next_offset = after_offset
            latest_offset = max(after_offset, terminal_session.next_output_offset)
            driver = self._driver
            if driver is not None:
                resource_snapshot = driver.resource_snapshot()
                output_buffer = resource_snapshot.output_buffer
                latest_offset = max(after_offset, output_buffer.next_offset)
                if after_offset <= output_buffer.next_offset:
                    read_start = max(
                        after_offset,
                        output_buffer.oldest_offset,
                        latest_offset - TERMINAL_WATCH_MAX_OUTPUT_BYTES,
                    )
                    output_read = driver.read_output(read_start, TERMINAL_WATCH_MAX_OUTPUT_BYTES)
                    output_bytes = output_read.data
                    start_offset = output_read.start_offset
                    next_offset = output_read.next_offset
                    latest_offset = max(latest_offset, output_read.latest_offset)
            output = output_bytes.decode("utf-8", errors="replace")
            session_status = self._status.value
            event = "process_exit" if self._status is TerminalSessionStatus.EXITED else "terminal_error"
            content: dict[str, Any] = {
                "terminal_session_id": self.terminal_session_id,
                "event": event,
                "after_offset": after_offset,
                "session_status": session_status,
                "exit_code": self._exit_code,
                "failure_reason": self._failure_reason,
                "output": output,
                "start_offset": start_offset,
                "next_offset": next_offset,
                "latest_offset": latest_offset,
                "output_truncated": start_offset > after_offset or next_offset < latest_offset,
                "eof": True,
                "condition": "process_exit",
                "matched": self._status is TerminalSessionStatus.EXITED,
            }

            watch_results: dict[str, dict[str, Any]] = {}
            active_subscriptions = [watch for watch in watches if watch.status in active_statuses and isinstance(watch.extra, dict) and watch.extra.get("terminal_watch_subscription") is True and isinstance(watch.id, int) and not isinstance(watch.id, bool)]
            for subscription in active_subscriptions:
                subscription_arguments = subscription.arguments if isinstance(subscription.arguments, dict) else {}
                subscription_after_offset = subscription_arguments.get("after_offset", 0)
                if not isinstance(subscription_after_offset, int) or isinstance(subscription_after_offset, bool) or subscription_after_offset < 0:
                    subscription_after_offset = 0
                subscription_condition = subscription_arguments.get("condition")
                subscription_max_bytes = subscription_arguments.get("max_bytes", TERMINAL_WATCH_MAX_OUTPUT_BYTES)
                subscription_output_bytes = b""
                subscription_start_offset = subscription_after_offset
                subscription_next_offset = subscription_after_offset
                if subscription_after_offset < start_offset:
                    subscription_start_offset = start_offset
                    subscription_output_bytes = output_bytes
                elif subscription_after_offset < next_offset:
                    subscription_start_offset = subscription_after_offset
                    output_start_index = subscription_after_offset - start_offset
                    subscription_output_bytes = output_bytes[output_start_index:]
                if len(subscription_output_bytes) > subscription_max_bytes:
                    available_end_offset = subscription_start_offset + len(subscription_output_bytes)
                    subscription_output_bytes = subscription_output_bytes[-subscription_max_bytes:]
                    subscription_start_offset = available_end_offset - len(subscription_output_bytes)
                subscription_next_offset = subscription_start_offset + len(subscription_output_bytes)
                subscription_latest_offset = max(latest_offset, subscription_after_offset)
                subscription_output = subscription_output_bytes.decode("utf-8", errors="replace")
                subscription_matched = (subscription_condition == "process_exit" and self._status is TerminalSessionStatus.EXITED) or (subscription_condition == "output_contains" and isinstance(subscription_arguments.get("text"), str) and subscription_arguments["text"] in subscription_output)
                child_content = dict(content)
                child_content.update(
                    {
                        "after_offset": subscription_after_offset,
                        "condition": subscription_condition,
                        "matched": subscription_matched,
                        "output": subscription_output,
                        "start_offset": subscription_start_offset,
                        "next_offset": subscription_next_offset,
                        "latest_offset": subscription_latest_offset,
                        "output_truncated": subscription_start_offset > subscription_after_offset or subscription_next_offset < subscription_latest_offset,
                    }
                )
                watch_results[str(subscription.id)] = child_content
            content["watch_results"] = watch_results

            snapshot_data = TerminalSessionManager._build_terminal_snapshot(terminal_session).model_dump(mode="json")

            command_results: dict[str, dict[str, Any]] = {}
            last_completed_command_id = getattr(self, "_last_completed_command_id", None)
            for command in commands:
                if command.id is None or not (
                    command.status
                    in {
                        TerminalControlCommandStatus.PENDING,
                        TerminalControlCommandStatus.PROCESSING,
                    }
                    or command.id == last_completed_command_id
                ):
                    continue

                result: dict[str, Any] | None = None
                error: str | None = None
                command_result_status = "succeeded"
                if command.status == TerminalControlCommandStatus.SUCCEEDED:
                    result = command.result
                elif command.status == TerminalControlCommandStatus.FAILED:
                    command_result_status = "failed"
                    error = command.error
                else:
                    try:
                        action = TerminalAction(command.action)
                    except (TypeError, ValueError):
                        action = None
                    try:
                        if action is TerminalAction.READ:
                            result = await self._execute_command(command)
                        elif action is TerminalAction.CLOSE:
                            result = self._close_result()
                        else:
                            raise RuntimeError(t(ERR_TERMINAL_PTY_CLOSED))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        command_result_status = "failed"
                        error = str(exc)

                command_results[str(command.id)] = {
                    "status": command_result_status,
                    "result": result,
                    "error": error,
                    "snapshot": snapshot_data,
                }

            await persist_terminal_completion(
                db,
                terminal_session=terminal_session,
                content=content,
                command_results=command_results,
            )
            await cleanup_terminal_session(
                db,
                terminal_session=terminal_session,
                worker_id=self.worker_id,
                process_closed=True,
                cancel_watch=False,
                failure_reason=self._failure_reason or self._stop_reason or t(ERR_TERMINAL_PTY_CLOSED),
            )
            await db.commit()

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
