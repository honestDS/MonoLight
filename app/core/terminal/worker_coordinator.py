import asyncio
import uuid

from app.core.constants import (
    ERR_TERMINAL_SESSION_LEASE_LOST,
    ERR_TERMINAL_WORKER_STOPPED,
)
from app.core.crud.terminal.session import (
    terminal_session_crud,
)
from app.core.i18n import t
from app.core.terminal.recovery import cleanup_terminal_process_identity
from app.core.terminal.schemas import (
    TerminalSessionStatus,
)
from app.models.terminal_session import TerminalSession
from app.providers.database import AsyncSessionLocal

from .audit_lifecycle import _update_terminal_confirmation_status, finalize_terminal_session_audit
from .manager_common import (
    TERMINAL_FINAL_CLAIM_CLEANUP_INTERVAL_SECONDS,
    TERMINAL_SESSION_LEASE_RENEW_INTERVAL_SECONDS,
    TERMINAL_SESSION_LEASE_SECONDS,
    TERMINAL_SESSION_POLL_INTERVAL_SECONDS,
    logger,
)
from .session_runtime import _TerminalSessionRuntime
from .session_runtime_common import (
    _terminal_output_buffer,
    _TerminalLeaseLost,
)

__all__ = [
    "TerminalWorkerCoordinator",
]


class TerminalWorkerCoordinator:
    def __init__(self) -> None:
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._worker_id = uuid.uuid4().hex
        self._owned_session_ids: set[str] = set()
        self._runtime_tasks: dict[str, asyncio.Task[None]] = {}
        self._runtimes: dict[str, _TerminalSessionRuntime] = {}
        self._last_lease_renewal_at = 0.0
        self._last_final_claim_cleanup_at = 0.0

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is None:
            await self._shutdown_runtimes()
            return
        try:
            await task
        finally:
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        self._last_lease_renewal_at = loop.time()
        try:
            while not self._stop_event.is_set():
                claimed = None
                recoverable = None
                try:
                    now = loop.time()
                    if now - self._last_lease_renewal_at >= TERMINAL_SESSION_LEASE_RENEW_INTERVAL_SECONDS:
                        for terminal_session_id in tuple(self._owned_session_ids):
                            runtime = self._runtimes.get(terminal_session_id)
                            try:
                                async with AsyncSessionLocal() as db:
                                    renewed = await terminal_session_crud.renew_lease(
                                        db,
                                        terminal_session_id,
                                        self._worker_id,
                                        TERMINAL_SESSION_LEASE_SECONDS,
                                    )
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                if runtime is not None:
                                    runtime.request_stop(t(ERR_TERMINAL_SESSION_LEASE_LOST), lease_lost=True)
                                    try:
                                        await runtime.force_close()
                                    except asyncio.CancelledError:
                                        raise
                                    except Exception:
                                        logger.exception(
                                            "Terminal session lease-loss cleanup failed",
                                            extra={"terminal_session_id": terminal_session_id},
                                        )
                                logger.exception(
                                    "Terminal session lease renewal failed",
                                    extra={"terminal_session_id": terminal_session_id},
                                )
                            else:
                                if not renewed:
                                    if runtime is None:
                                        self._owned_session_ids.discard(terminal_session_id)
                                    else:
                                        runtime.request_stop(t(ERR_TERMINAL_SESSION_LEASE_LOST), lease_lost=True)
                                        try:
                                            await runtime.force_close()
                                        except asyncio.CancelledError:
                                            raise
                                        except Exception:
                                            logger.exception(
                                                "Terminal session lease-loss cleanup failed",
                                                extra={"terminal_session_id": terminal_session_id},
                                            )
                        self._last_lease_renewal_at = now

                    if now - self._last_final_claim_cleanup_at >= TERMINAL_FINAL_CLAIM_CLEANUP_INTERVAL_SECONDS:
                        async with AsyncSessionLocal() as db:
                            await terminal_session_crud.cleanup_expired_final_claims(db)
                        self._last_final_claim_cleanup_at = now

                    async with AsyncSessionLocal() as db:
                        recoverable = await terminal_session_crud.claim_next_recoverable(
                            db,
                            self._worker_id,
                            TERMINAL_SESSION_LEASE_SECONDS,
                        )
                    if recoverable is not None:
                        await self._recover_session(recoverable)
                        claimed = recoverable
                    else:
                        async with AsyncSessionLocal() as db:
                            claimed = await terminal_session_crud.claim_next_starting(
                                db,
                                self._worker_id,
                                TERMINAL_SESSION_LEASE_SECONDS,
                            )
                    if claimed is not None and recoverable is None:
                        terminal_session_id = claimed.terminal_session_id
                        if terminal_session_id not in self._runtime_tasks:
                            runtime = _TerminalSessionRuntime(claimed, self._worker_id)
                            self._owned_session_ids.add(terminal_session_id)
                            self._runtimes[terminal_session_id] = runtime
                            self._runtime_tasks[terminal_session_id] = asyncio.create_task(self._run_session(runtime))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Terminal worker coordinator loop failed")

                if claimed is None:
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=TERMINAL_SESSION_POLL_INTERVAL_SECONDS,
                        )
                    except TimeoutError:
                        pass
        finally:
            await self._shutdown_runtimes()

    async def _run_session(self, runtime: _TerminalSessionRuntime) -> None:
        try:
            await runtime.run()
        except asyncio.CancelledError:
            raise
        except _TerminalLeaseLost:
            pass
        except Exception:
            logger.exception(
                "Terminal session runtime failed",
                extra={"terminal_session_id": runtime.terminal_session_id},
            )
        finally:
            terminal_session_id = runtime.terminal_session_id
            self._owned_session_ids.discard(terminal_session_id)
            self._runtimes.pop(terminal_session_id, None)
            self._runtime_tasks.pop(terminal_session_id, None)

    async def _recover_session(self, terminal_session: TerminalSession) -> None:
        cleanup_result = await cleanup_terminal_process_identity(terminal_session.process_identity)
        if cleanup_result.errors:
            logger.error(
                "Terminal session orphan process cleanup failed",
                extra={
                    "terminal_session_id": terminal_session.terminal_session_id,
                    "errors": cleanup_result.errors,
                },
            )

        failure_reason = t(ERR_TERMINAL_SESSION_LEASE_LOST)
        audit_record_id: int | None = None
        async with AsyncSessionLocal() as db:
            updated = await terminal_session_crud.update_runtime_snapshot(
                db,
                terminal_session.terminal_session_id,
                self._worker_id,
                TerminalSessionStatus.LOST,
                _terminal_output_buffer(terminal_session),
                failure_reason=failure_reason,
                process_identity=terminal_session.process_identity,
                commit=False,
            )
            if not updated:
                await db.rollback()
                return
            audit_record_id = await finalize_terminal_session_audit(
                db,
                terminal_session,
                status=TerminalSessionStatus.LOST,
                exit_code=None,
                failure_reason=failure_reason,
            )
            await terminal_session_crud.fail_unfinished_commands(
                db,
                terminal_session.terminal_session_id,
                failure_reason,
                worker_id=self._worker_id,
                commit=False,
            )
            await terminal_session_crud.release_claim(
                db,
                terminal_session.terminal_session_id,
                self._worker_id,
                commit=False,
            )
            await db.commit()

        if audit_record_id is not None:
            async with AsyncSessionLocal() as db:
                try:
                    await _update_terminal_confirmation_status(db, audit_record_id=audit_record_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Terminal session confirmation status projection failed",
                        extra={
                            "terminal_session_id": terminal_session.terminal_session_id,
                            "audit_record_id": audit_record_id,
                        },
                    )

    async def _shutdown_runtimes(self) -> None:
        owned_session_ids = tuple(self._owned_session_ids)
        runtimes = tuple(self._runtimes.values())
        shutdown_cancelled: asyncio.CancelledError | None = None
        for runtime in runtimes:
            runtime.request_stop(t(ERR_TERMINAL_WORKER_STOPPED))
        if runtimes:
            close_results = await asyncio.gather(
                *(runtime.force_close() for runtime in runtimes),
                return_exceptions=True,
            )
            for runtime, result in zip(runtimes, close_results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    shutdown_cancelled = result
                    continue
                if isinstance(result, BaseException):
                    logger.exception(
                        "Terminal session force close failed",
                        exc_info=(type(result), result, result.__traceback__),
                        extra={"terminal_session_id": runtime.terminal_session_id},
                    )

        tasks = tuple(self._runtime_tasks.values())
        if tasks:
            task_results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, task_results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    shutdown_cancelled = result
                    continue
                if isinstance(result, BaseException):
                    logger.exception(
                        "Terminal session runtime task failed",
                        exc_info=(type(result), result, result.__traceback__),
                    )

        if owned_session_ids:
            async with AsyncSessionLocal() as db:
                for terminal_session_id in owned_session_ids:
                    await terminal_session_crud.release_claim(
                        db,
                        terminal_session_id,
                        self._worker_id,
                        commit=False,
                    )
                await db.commit()

        self._owned_session_ids.clear()
        self._runtimes.clear()
        self._runtime_tasks.clear()
        if shutdown_cancelled is not None:
            raise shutdown_cancelled
