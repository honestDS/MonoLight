from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_FIELD_INVALID,
    ERR_MEMORY_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED,
    ERR_MEMORY_JOB_RENEW_INTERVAL_INVALID,
    ERR_MEMORY_JOB_UNEXPECTED_FAILURE,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.memory_jobs.executor import (
    MemoryJobCancelledError,
    MemoryJobDeterministicError,
    MemoryJobExecutor,
    MemoryJobLeaseLostError,
    MemoryJobRetryableError,
    SessionFactory,
)
from app.core.memory_jobs.maintenance_lifecycle import finalize_maintenance_terminal_state
from app.core.memory_jobs.manager import best_effort_submit_auto_organization_after_publication
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
)
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

MEMORY_JOB_POLL_INTERVAL_SECONDS = 0.2
MEMORY_JOB_LEASE_SECONDS = 60
MEMORY_JOB_LEASE_RENEW_INTERVAL_SECONDS = 20
MEMORY_JOB_RECOVERY_INTERVAL_SECONDS = 10
MEMORY_JOB_MAX_CONCURRENCY = 4
MEMORY_JOB_RECOVERY_RETRY_DELAY_SECONDS = 1
MEMORY_JOB_SHUTDOWN_RETRY_DELAY_SECONDS = 1
MEMORY_JOB_RETRY_MIN_SECONDS = 1
MEMORY_JOB_RETRY_MAX_SECONDS = 300
_AUTO_ORGANIZATION_TRIGGER_OPERATIONS = frozenset(
    {
        LongTermMemoryMutationOperation.CREATE,
        LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
        LongTermMemoryMutationOperation.UPDATE,
    }
)


def retry_delay_seconds(attempt_count: int) -> int:
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int):
        raise TypeError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="attempt_count"))
    if attempt_count < 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_NON_NEGATIVE, field="attempt_count"))
    if attempt_count <= 1:
        return MEMORY_JOB_RETRY_MIN_SECONDS
    if attempt_count >= 10:
        return MEMORY_JOB_RETRY_MAX_SECONDS
    return min(MEMORY_JOB_RETRY_MAX_SECONDS, 2 ** (attempt_count - 1))


@dataclass(slots=True)
class _RunningJob:
    uid: str
    worker_id: str
    task: asyncio.Task[None]


def _validate_positive_number(value: Any, *, field: str) -> Real:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value) or value <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field=field))
    return value


def _validate_positive_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field=field))
    return value


class MemoryJobConsumer:
    __slots__ = (
        "_executor",
        "_session_factory",
        "_poll_interval_seconds",
        "_lease_seconds",
        "_renew_interval_seconds",
        "_recovery_interval_seconds",
        "_max_concurrency",
        "_recovery_retry_delay_seconds",
        "_shutdown_retry_delay_seconds",
        "_stop_event",
        "_task",
        "_running",
        "_last_recovery_at",
        "_last_lease_renewal_at",
    )

    def __init__(
        self,
        executor: MemoryJobExecutor,
        session_factory: SessionFactory = AsyncSessionLocal,
        *,
        poll_interval_seconds: Real = MEMORY_JOB_POLL_INTERVAL_SECONDS,
        lease_seconds: Real = MEMORY_JOB_LEASE_SECONDS,
        renew_interval_seconds: Real = MEMORY_JOB_LEASE_RENEW_INTERVAL_SECONDS,
        recovery_interval_seconds: Real = MEMORY_JOB_RECOVERY_INTERVAL_SECONDS,
        max_concurrency: int = MEMORY_JOB_MAX_CONCURRENCY,
        recovery_retry_delay_seconds: Real = MEMORY_JOB_RECOVERY_RETRY_DELAY_SECONDS,
        shutdown_retry_delay_seconds: Real = MEMORY_JOB_SHUTDOWN_RETRY_DELAY_SECONDS,
    ) -> None:
        self._poll_interval_seconds = _validate_positive_number(poll_interval_seconds, field="poll_interval_seconds")
        self._lease_seconds = _validate_positive_number(lease_seconds, field="lease_seconds")
        self._renew_interval_seconds = _validate_positive_number(renew_interval_seconds, field="renew_interval_seconds")
        self._recovery_interval_seconds = _validate_positive_number(recovery_interval_seconds, field="recovery_interval_seconds")
        self._max_concurrency = _validate_positive_integer(max_concurrency, field="max_concurrency")
        self._recovery_retry_delay_seconds = _validate_positive_number(
            recovery_retry_delay_seconds,
            field="recovery_retry_delay_seconds",
        )
        self._shutdown_retry_delay_seconds = _validate_positive_number(
            shutdown_retry_delay_seconds,
            field="shutdown_retry_delay_seconds",
        )
        if self._renew_interval_seconds >= self._lease_seconds:
            raise ValueError(t(ERR_MEMORY_JOB_RENEW_INTERVAL_INVALID))

        self._executor = executor
        self._session_factory = session_factory
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running: dict[int, _RunningJob] = {}
        self._last_recovery_at = 0.0
        self._last_lease_renewal_at = 0.0

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._last_recovery_at = 0.0
        self._last_lease_renewal_at = 0.0
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        loop_task = self._task
        if loop_task is not None and not loop_task.done():
            loop_task.cancel()

        running_tasks = [entry.task for entry in self._running.values() if not entry.task.done()]
        for task in running_tasks:
            task.cancel()

        tasks = [task for task in (loop_task, *running_tasks) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._running.clear()
        self._last_recovery_at = 0.0
        self._last_lease_renewal_at = 0.0
        if self._task is loop_task:
            self._task = None

    async def run_once(self) -> int:
        now = asyncio.get_running_loop().time()
        if now - self._last_recovery_at >= self._recovery_interval_seconds:
            self._last_recovery_at = now
            await self._recover_expired()
        if now - self._last_lease_renewal_at >= self._renew_interval_seconds:
            self._last_lease_renewal_at = now
            await self._renew_running_jobs()
        return await self._claim_available_jobs()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            claimed_count = 0
            try:
                claimed_count = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_loop_exception(exc)

            if claimed_count == 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval_seconds)
                except TimeoutError:
                    pass

    async def _recover_expired(self) -> None:
        try:
            async with self._session_factory() as db:
                recovery = await memory_job_crud.recover_expired(
                    db,
                    delay_seconds=self._recovery_retry_delay_seconds,
                    max_attempts_error=t(ERR_MEMORY_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED),
                    commit=False,
                )
                for terminal in recovery.terminal_jobs:
                    await finalize_maintenance_terminal_state(
                        db,
                        job=terminal.job,
                        status=terminal.status,
                        error=terminal.error,
                    )
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_database_exception(None, None, None, exc)

    async def _renew_running_jobs(self) -> None:
        for job_id, entry in tuple(self._running.items()):
            if entry.task.done():
                continue
            renewed = False
            try:
                async with self._session_factory() as db:
                    renewed = await memory_job_crud.renew_lease(
                        db,
                        uid=entry.uid,
                        job_id=job_id,
                        owner=entry.worker_id,
                        lease_seconds=self._lease_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_database_exception(entry.uid, job_id, entry.worker_id, exc)

            if not renewed:
                self._cancel_running_job(job_id, entry)

    def _cancel_running_job(self, job_id: int, entry: _RunningJob) -> None:
        current = self._running.get(job_id)
        if current is entry and not entry.task.done():
            entry.task.cancel()

    async def _claim_available_jobs(self) -> int:
        available_slots = self._max_concurrency - sum(not entry.task.done() for entry in self._running.values())
        if available_slots <= 0:
            return 0

        enabled_operations = self._executor.enabled_operations
        try:
            async with self._session_factory() as db:
                candidates = await memory_job_crud.list_claimable_for_worker(
                    db,
                    enabled_operations=enabled_operations,
                    limit=available_slots,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_database_exception(None, None, None, exc)
            return 0

        claimed_count = 0
        for candidate in candidates:
            job_id = candidate.id
            if job_id is None or not candidate.uid:
                continue
            worker_id = uuid.uuid4().hex
            try:
                async with self._session_factory() as db:
                    claimed_job = await memory_job_crud.try_claim(
                        db,
                        uid=candidate.uid,
                        job_id=job_id,
                        owner=worker_id,
                        lease_seconds=self._lease_seconds,
                        enabled_operations=enabled_operations,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_database_exception(candidate.uid, job_id, worker_id, exc)
                continue

            if claimed_job is None or claimed_job.id is None:
                continue
            task = asyncio.create_task(self._run_claimed(claimed_job, worker_id))
            self._running[claimed_job.id] = _RunningJob(
                uid=claimed_job.uid,
                worker_id=worker_id,
                task=task,
            )
            task.add_done_callback(lambda done_task, claimed_id=claimed_job.id: self._on_task_done(claimed_id, done_task))
            claimed_count += 1
        return claimed_count

    async def _run_claimed(self, claimed_job: LongTermMemoryMutationJob, worker_id: str) -> None:
        try:
            try:
                execution_result = await self._executor.execute_claimed(claimed_job, worker_id)
            except MemoryJobLeaseLostError as exc:
                self._log_job_exception(claimed_job, worker_id, exc, t("LOG_MEMORY_JOB_LEASE_LOST"))
            except MemoryJobCancelledError as exc:
                self._log_job_exception(claimed_job, worker_id, exc, t("LOG_MEMORY_JOB_CANCELLED"))
                await self._mark_cancelled(claimed_job, worker_id)
            except MemoryJobDeterministicError as exc:
                self._log_job_exception(claimed_job, worker_id, exc, t("LOG_MEMORY_JOB_EXECUTION_FAILED"))
                await self._handle_failure(
                    claimed_job,
                    worker_id,
                    error=exc.safe_message,
                    result=exc.result,
                )
            except MemoryJobRetryableError as exc:
                self._log_job_exception(claimed_job, worker_id, exc, t("LOG_MEMORY_JOB_EXECUTION_FAILED"))
                await self._handle_retryable_failure(
                    claimed_job,
                    worker_id,
                    error=exc.safe_message,
                    result=exc.result,
                )
            except Exception as exc:
                self._log_job_exception(claimed_job, worker_id, exc, t("LOG_MEMORY_JOB_EXECUTION_FAILED"))
                await self._handle_retryable_failure(
                    claimed_job,
                    worker_id,
                    error=t(ERR_MEMORY_JOB_UNEXPECTED_FAILURE),
                    result=None,
                )
            else:
                if execution_result.finalized:
                    if claimed_job.operation in _AUTO_ORGANIZATION_TRIGGER_OPERATIONS:
                        await best_effort_submit_auto_organization_after_publication(
                            self._session_factory,
                            claimed_job.uid,
                            claimed_job.id,
                        )
                else:
                    marked = await self._mark_succeeded(claimed_job, worker_id, execution_result.result)
                    if not marked:
                        await self._mark_cancelled(claimed_job, worker_id)
                    elif claimed_job.operation in _AUTO_ORGANIZATION_TRIGGER_OPERATIONS:
                        await best_effort_submit_auto_organization_after_publication(
                            self._session_factory,
                            claimed_job.uid,
                            claimed_job.id,
                        )
        except asyncio.CancelledError:
            await self._release_claim_for_shutdown(claimed_job, worker_id)
            raise
        except Exception as exc:
            self._log_job_exception(claimed_job, worker_id, exc, t("LOG_MEMORY_JOB_STATE_UPDATE_FAILED"))

    async def _handle_failure(
        self,
        job: LongTermMemoryMutationJob,
        worker_id: str,
        *,
        error: str,
        result: dict[str, Any] | None,
    ) -> None:
        marked = await self._mark_failed(job, worker_id, error=error, result=result)
        if not marked:
            await self._mark_cancelled(job, worker_id)

    async def _handle_retryable_failure(
        self,
        job: LongTermMemoryMutationJob,
        worker_id: str,
        *,
        error: str,
        result: dict[str, Any] | None,
    ) -> None:
        if job.attempt_count >= job.max_attempts:
            await self._handle_failure(job, worker_id, error=error, result=result)
            return

        released = await self._release_for_retry(
            job,
            worker_id,
            error=error,
            delay_seconds=retry_delay_seconds(job.attempt_count),
        )
        if not released:
            await self._mark_cancelled(job, worker_id)

    async def _mark_succeeded(
        self,
        job: LongTermMemoryMutationJob,
        worker_id: str,
        result: dict[str, Any] | None,
    ) -> bool:
        async with self._session_factory() as db:
            return await memory_job_crud.mark_succeeded(
                db,
                uid=job.uid,
                job_id=job.id,
                owner=worker_id,
                result=result,
            )

    async def _mark_failed(
        self,
        job: LongTermMemoryMutationJob,
        worker_id: str,
        *,
        error: str,
        result: dict[str, Any] | None,
    ) -> bool:
        async with self._session_factory() as db:
            changed = await memory_job_crud.mark_failed(
                db,
                uid=job.uid,
                job_id=job.id,
                owner=worker_id,
                error=error,
                result=result,
                commit=False,
            )
            if changed:
                await finalize_maintenance_terminal_state(
                    db,
                    job=job,
                    status=LongTermMemoryMutationStatus.FAILED,
                    error=error,
                )
            await db.commit()
            return changed

    async def _mark_cancelled(self, job: LongTermMemoryMutationJob, worker_id: str) -> bool:
        async with self._session_factory() as db:
            changed = await memory_job_crud.mark_cancelled(
                db,
                uid=job.uid,
                job_id=job.id,
                owner=worker_id,
                commit=False,
            )
            if changed:
                await finalize_maintenance_terminal_state(
                    db,
                    job=job,
                    status=LongTermMemoryMutationStatus.CANCELLED,
                    error=t("ERR_MEMORY_JOB_CANCELLATION_REQUESTED"),
                )
            await db.commit()
            return changed

    async def _release_for_retry(
        self,
        job: LongTermMemoryMutationJob,
        worker_id: str,
        *,
        error: str,
        delay_seconds: int,
    ) -> bool:
        async with self._session_factory() as db:
            return await memory_job_crud.release_for_retry(
                db,
                uid=job.uid,
                job_id=job.id,
                owner=worker_id,
                error=error,
                delay_seconds=delay_seconds,
            )

    async def _release_claim_for_shutdown(self, job: LongTermMemoryMutationJob, worker_id: str) -> None:
        release_task = asyncio.create_task(self._release_claim(job, worker_id))
        try:
            await asyncio.shield(release_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(release_task)
            except asyncio.CancelledError:
                await asyncio.gather(release_task, return_exceptions=True)
                raise
            except Exception as exc:
                self._log_database_exception(job.uid, job.id, worker_id, exc)
        except Exception as exc:
            self._log_database_exception(job.uid, job.id, worker_id, exc)

    async def _release_claim(self, job: LongTermMemoryMutationJob, worker_id: str) -> None:
        async with self._session_factory() as db:
            changed = await memory_job_crud.release_claim_for_shutdown(
                db,
                uid=job.uid,
                job_id=job.id,
                owner=worker_id,
                delay_seconds=self._shutdown_retry_delay_seconds,
                max_attempts_error=t(ERR_MEMORY_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED),
                commit=False,
            )
            if changed:
                current = await memory_job_crud.get_by_id(db, uid=job.uid, job_id=job.id)
                if current is not None and current.status in {
                    LongTermMemoryMutationStatus.FAILED,
                    LongTermMemoryMutationStatus.CANCELLED,
                }:
                    await finalize_maintenance_terminal_state(
                        db,
                        job=job,
                        status=current.status,
                        error=current.error,
                    )
            await db.commit()

    def _on_task_done(self, job_id: int, task: asyncio.Task[None]) -> None:
        try:
            task_exception = task.exception()
        except asyncio.CancelledError:
            task_exception = None
        if task_exception is not None:
            entry = self._running.get(job_id)
            self._log_database_exception(
                entry.uid if entry is not None and entry.task is task else None,
                job_id,
                entry.worker_id if entry is not None and entry.task is task else None,
                task_exception,
            )

        entry = self._running.get(job_id)
        if entry is not None and entry.task is task:
            self._running.pop(job_id, None)

    @staticmethod
    def _log_job_exception(
        job: LongTermMemoryMutationJob,
        worker_id: str,
        exc: BaseException,
        message: str,
    ) -> None:
        logger.bind(
            uid=job.uid,
            job_id=job.id,
            worker_id=worker_id,
            exception_type=type(exc).__name__,
        ).warning(message)

    @staticmethod
    def _log_database_exception(
        uid: str | None,
        job_id: int | None,
        worker_id: str | None,
        exc: BaseException,
    ) -> None:
        logger.bind(
            uid=uid,
            job_id=job_id,
            worker_id=worker_id,
            exception_type=type(exc).__name__,
        ).error(t("LOG_MEMORY_JOB_DATABASE_OPERATION_FAILED"))

    @staticmethod
    def _log_loop_exception(exc: BaseException) -> None:
        logger.bind(exception_type=type(exc).__name__).error(t("LOG_MEMORY_JOB_LOOP_FAILED"))


def create_memory_job_consumer(
    executor: MemoryJobExecutor | None = None,
    **kwargs: Any,
) -> MemoryJobConsumer:
    if executor is None:
        from app.core.memory_jobs.handlers import (
            create_default_memory_job_executor,
            default_memory_job_executor,
        )

        if "session_factory" in kwargs:
            executor = create_default_memory_job_executor(session_factory=kwargs["session_factory"])
        else:
            executor = default_memory_job_executor
    return MemoryJobConsumer(executor=executor, **kwargs)


__all__ = [
    "MemoryJobConsumer",
    "create_memory_job_consumer",
    "retry_delay_seconds",
]
