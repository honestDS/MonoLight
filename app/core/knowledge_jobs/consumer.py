from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from math import isfinite
from numbers import Real

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED,
    ERR_KNOWLEDGE_JOB_RENEW_INTERVAL_INVALID,
    ERR_KNOWLEDGE_JOB_UNEXPECTED_FAILURE,
    ERR_VALUE_MUST_BE_POSITIVE,
    LOG_KNOWLEDGE_JOB_CANCELLED,
    LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED,
    LOG_KNOWLEDGE_JOB_EXECUTION_FAILED,
    LOG_KNOWLEDGE_JOB_LEASE_LOST,
    LOG_KNOWLEDGE_JOB_LOOP_FAILED,
    LOG_KNOWLEDGE_JOB_STARTUP_RECOVERY_COMPLETED,
    LOG_KNOWLEDGE_JOB_STATE_UPDATE_FAILED,
)
from app.core.crud.knowledge.job import (
    KnowledgeJobRecoveryResult,
    is_system_cleanup_operation,
    knowledge_job_crud,
)
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobCancelledError,
    KnowledgeJobDeterministicError,
    KnowledgeJobExecutionError,
    KnowledgeJobExecutor,
    KnowledgeJobLeaseLostError,
    KnowledgeJobRetryableError,
    SessionFactory,
)
from app.core.knowledge_jobs.handlers import create_default_knowledge_job_executor
from app.core.knowledge_jobs.migration import (
    cleanup_terminal_target_collection,
    finalize_knowledge_migration_terminal_state,
)
from app.core.log import get_logger
from app.models.knowledge_base import KnowledgeJob
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

KNOWLEDGE_JOB_POLL_INTERVAL_SECONDS = 0.2
KNOWLEDGE_JOB_LEASE_SECONDS = 60
KNOWLEDGE_JOB_LEASE_RENEW_INTERVAL_SECONDS = 20
KNOWLEDGE_JOB_RECOVERY_INTERVAL_SECONDS = 10
KNOWLEDGE_JOB_MAX_CONCURRENCY = 4
KNOWLEDGE_JOB_RECOVERY_RETRY_DELAY_SECONDS = 1
KNOWLEDGE_JOB_SHUTDOWN_RETRY_DELAY_SECONDS = 1
KNOWLEDGE_JOB_RETRY_MAX_SECONDS = 300


def retry_delay_seconds(attempt_count: int) -> int:
    if attempt_count <= 1:
        return 1
    if attempt_count >= 10:
        return KNOWLEDGE_JOB_RETRY_MAX_SECONDS
    return min(KNOWLEDGE_JOB_RETRY_MAX_SECONDS, 2 ** (attempt_count - 1))


def _positive_number(value: Real, *, field: str) -> Real:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value) or value <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field=field))
    return value


def _positive_integer(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field=field))
    return value


@dataclass(slots=True)
class _RunningJob:
    uid: str
    worker_id: str
    task: asyncio.Task[None]


class KnowledgeJobConsumer:
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
        executor: KnowledgeJobExecutor,
        session_factory: SessionFactory = AsyncSessionLocal,
        *,
        poll_interval_seconds: Real = KNOWLEDGE_JOB_POLL_INTERVAL_SECONDS,
        lease_seconds: Real = KNOWLEDGE_JOB_LEASE_SECONDS,
        renew_interval_seconds: Real = KNOWLEDGE_JOB_LEASE_RENEW_INTERVAL_SECONDS,
        recovery_interval_seconds: Real = KNOWLEDGE_JOB_RECOVERY_INTERVAL_SECONDS,
        max_concurrency: int = KNOWLEDGE_JOB_MAX_CONCURRENCY,
        recovery_retry_delay_seconds: Real = KNOWLEDGE_JOB_RECOVERY_RETRY_DELAY_SECONDS,
        shutdown_retry_delay_seconds: Real = KNOWLEDGE_JOB_SHUTDOWN_RETRY_DELAY_SECONDS,
    ) -> None:
        self._poll_interval_seconds = _positive_number(poll_interval_seconds, field="poll_interval_seconds")
        self._lease_seconds = _positive_number(lease_seconds, field="lease_seconds")
        self._renew_interval_seconds = _positive_number(renew_interval_seconds, field="renew_interval_seconds")
        self._recovery_interval_seconds = _positive_number(recovery_interval_seconds, field="recovery_interval_seconds")
        self._max_concurrency = _positive_integer(max_concurrency, field="max_concurrency")
        self._recovery_retry_delay_seconds = _positive_number(recovery_retry_delay_seconds, field="recovery_retry_delay_seconds")
        self._shutdown_retry_delay_seconds = _positive_number(shutdown_retry_delay_seconds, field="shutdown_retry_delay_seconds")
        if self._renew_interval_seconds >= self._lease_seconds:
            raise ValueError(t(ERR_KNOWLEDGE_JOB_RENEW_INTERVAL_INVALID))
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
        self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            startup_recovery = await self._recover_expired()
            if startup_recovery is not None:
                self._last_recovery_at = loop.time()
                logger.info(
                    t(
                        LOG_KNOWLEDGE_JOB_STARTUP_RECOVERY_COMPLETED,
                        retried=startup_recovery.retried,
                        failed=startup_recovery.failed,
                        cancelled=startup_recovery.cancelled,
                    )
                )
            else:
                self._last_recovery_at = loop.time() - self._recovery_interval_seconds
            while not self._stop_event.is_set():
                try:
                    now = loop.time()
                    if now - self._last_recovery_at >= self._recovery_interval_seconds:
                        await self._recover_expired()
                        self._last_recovery_at = now
                    if now - self._last_lease_renewal_at >= self._renew_interval_seconds:
                        await self._renew_running()
                        self._last_lease_renewal_at = now
                    await self._claim_available()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.bind(error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_LOOP_FAILED))
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=float(self._poll_interval_seconds))
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            return

    async def _recover_expired(self) -> KnowledgeJobRecoveryResult | None:
        try:
            target_collections: list[str] = []
            async with self._session_factory() as db:
                recovery = await knowledge_job_crud.recover_expired(
                    db,
                    delay_seconds=int(self._recovery_retry_delay_seconds),
                    max_attempts_error=t(ERR_KNOWLEDGE_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED),
                    commit=False,
                )
                for terminal in recovery.terminal_jobs:
                    if terminal.job.id is None:
                        continue
                    current = await knowledge_job_crud.get_by_id(
                        db,
                        uid=terminal.job.uid,
                        job_id=terminal.job.id,
                    )
                    if current is None:
                        continue
                    target_collection = await finalize_knowledge_migration_terminal_state(
                        db,
                        job=current,
                        error=terminal.error,
                    )
                    if target_collection:
                        target_collections.append(target_collection)
                await db.commit()
            for target_collection in target_collections:
                await cleanup_terminal_target_collection(target_collection)
            return recovery
        except Exception as exc:
            logger.bind(error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))
            return None

    async def _renew_running(self) -> None:
        for job_id, entry in list(self._running.items()):
            if entry.task.done():
                self._running.pop(job_id, None)
                continue
            try:
                async with self._session_factory() as db:
                    renewed = await knowledge_job_crud.renew_lease(
                        db,
                        uid=entry.uid,
                        job_id=job_id,
                        owner=entry.worker_id,
                        lease_seconds=int(self._lease_seconds),
                    )
            except Exception as exc:
                logger.bind(job_id=job_id, error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))
                continue
            if not renewed:
                entry.task.cancel()

    async def _claim_available(self) -> None:
        capacity = self._max_concurrency - len(self._running)
        if capacity <= 0 or not self._executor.enabled_operations:
            return
        async with self._session_factory() as db:
            candidates = await knowledge_job_crud.list_claimable_for_worker(
                db,
                enabled_operations=self._executor.enabled_operations,
                limit=max(capacity * 2, capacity),
            )
        for candidate in candidates:
            if capacity <= 0:
                break
            if candidate.id is None or candidate.id in self._running:
                continue
            worker_id = uuid.uuid4().hex
            async with self._session_factory() as db:
                claimed = await knowledge_job_crud.try_claim(
                    db,
                    uid=candidate.uid,
                    job_id=candidate.id,
                    owner=worker_id,
                    lease_seconds=int(self._lease_seconds),
                    enabled_operations=self._executor.enabled_operations,
                )
            if claimed is None:
                continue
            task = asyncio.create_task(self._execute(claimed, worker_id))
            self._running[candidate.id] = _RunningJob(uid=candidate.uid, worker_id=worker_id, task=task)
            task.add_done_callback(lambda _task, job_id=candidate.id: self._running.pop(job_id, None))
            capacity -= 1

    async def _execute(self, job: KnowledgeJob, worker_id: str) -> None:
        if job.id is None:
            return
        try:
            execution = await self._executor.execute_claimed(job, worker_id)
            if execution.finalized:
                return
            async with self._session_factory() as db:
                changed = await knowledge_job_crud.mark_succeeded(
                    db,
                    uid=job.uid,
                    job_id=job.id,
                    owner=worker_id,
                    result=execution.result,
                )
            if not changed:
                logger.bind(job_id=job.id, operation=str(job.operation)).warning(t(LOG_KNOWLEDGE_JOB_STATE_UPDATE_FAILED))
        except asyncio.CancelledError:
            await self._release_for_shutdown(job, worker_id)
            raise
        except KnowledgeJobLeaseLostError:
            logger.bind(job_id=job.id, operation=str(job.operation)).warning(t(LOG_KNOWLEDGE_JOB_LEASE_LOST))
        except KnowledgeJobCancelledError as exc:
            await self._mark_cancelled(job, worker_id, exc.safe_message)
        except KnowledgeJobDeterministicError as exc:
            await self._mark_failed(job, worker_id, exc.safe_message, exc.result)
        except KnowledgeJobRetryableError as exc:
            await self._retry_or_fail(job, worker_id, exc.safe_message, exc.result)
        except KnowledgeJobExecutionError as exc:
            await self._retry_or_fail(job, worker_id, exc.safe_message, exc.result)
        except Exception as exc:
            logger.bind(job_id=job.id, operation=str(job.operation), error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_EXECUTION_FAILED))
            await self._retry_or_fail(job, worker_id, t(ERR_KNOWLEDGE_JOB_UNEXPECTED_FAILURE), None)

    async def _retry_or_fail(self, job: KnowledgeJob, worker_id: str, safe_message: str, result: dict | None) -> None:
        if job.id is None:
            return
        try:
            target_collection = None
            async with self._session_factory() as db:
                if is_system_cleanup_operation(job.operation):
                    changed = await knowledge_job_crud.release_for_retry(
                        db,
                        uid=job.uid,
                        job_id=job.id,
                        owner=worker_id,
                        error=safe_message,
                        delay_seconds=retry_delay_seconds(job.attempt_count),
                        commit=False,
                    )
                elif job.attempt_count >= job.max_attempts:
                    changed = await knowledge_job_crud.mark_failed(
                        db,
                        uid=job.uid,
                        job_id=job.id,
                        owner=worker_id,
                        error=safe_message,
                        result=result,
                        commit=False,
                    )
                    if changed:
                        current = await knowledge_job_crud.get_by_id(db, uid=job.uid, job_id=job.id)
                        if current is not None:
                            target_collection = await finalize_knowledge_migration_terminal_state(
                                db,
                                job=current,
                                error=safe_message,
                            )
                else:
                    changed = await knowledge_job_crud.release_for_retry(
                        db,
                        uid=job.uid,
                        job_id=job.id,
                        owner=worker_id,
                        error=safe_message,
                        delay_seconds=retry_delay_seconds(job.attempt_count),
                        commit=False,
                    )
                await db.commit()
            await cleanup_terminal_target_collection(target_collection)
            if not changed:
                logger.bind(job_id=job.id, operation=str(job.operation)).warning(t(LOG_KNOWLEDGE_JOB_STATE_UPDATE_FAILED))
        except Exception as exc:
            logger.bind(job_id=job.id, error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))

    async def _mark_failed(self, job: KnowledgeJob, worker_id: str, safe_message: str, result: dict | None) -> None:
        if job.id is None:
            return
        try:
            target_collection = None
            async with self._session_factory() as db:
                changed = await knowledge_job_crud.mark_failed(
                    db,
                    uid=job.uid,
                    job_id=job.id,
                    owner=worker_id,
                    error=safe_message,
                    result=result,
                    commit=False,
                )
                if changed:
                    current = await knowledge_job_crud.get_by_id(db, uid=job.uid, job_id=job.id)
                    if current is not None:
                        target_collection = await finalize_knowledge_migration_terminal_state(
                            db,
                            job=current,
                            error=safe_message,
                        )
                await db.commit()
            await cleanup_terminal_target_collection(target_collection)
            if not changed:
                logger.bind(job_id=job.id, operation=str(job.operation)).warning(t(LOG_KNOWLEDGE_JOB_STATE_UPDATE_FAILED))
        except Exception as exc:
            logger.bind(job_id=job.id, error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))

    async def _mark_cancelled(self, job: KnowledgeJob, worker_id: str, safe_message: str) -> None:
        if job.id is None:
            return
        try:
            target_collection = None
            async with self._session_factory() as db:
                changed = await knowledge_job_crud.mark_cancelled(
                    db,
                    uid=job.uid,
                    job_id=job.id,
                    owner=worker_id,
                    error=safe_message,
                    commit=False,
                )
                if changed:
                    current = await knowledge_job_crud.get_by_id(db, uid=job.uid, job_id=job.id)
                    if current is not None:
                        target_collection = await finalize_knowledge_migration_terminal_state(
                            db,
                            job=current,
                            error=safe_message,
                        )
                await db.commit()
            await cleanup_terminal_target_collection(target_collection)
            if changed:
                logger.bind(job_id=job.id, operation=str(job.operation)).info(t(LOG_KNOWLEDGE_JOB_CANCELLED))
        except Exception as exc:
            logger.bind(job_id=job.id, error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))

    async def _release_for_shutdown(self, job: KnowledgeJob, worker_id: str) -> None:
        if job.id is None:
            return
        try:
            target_collection = None
            async with self._session_factory() as db:
                changed = await knowledge_job_crud.release_claim_for_shutdown(
                    db,
                    uid=job.uid,
                    job_id=job.id,
                    owner=worker_id,
                    delay_seconds=int(self._shutdown_retry_delay_seconds),
                    max_attempts_error=t(ERR_KNOWLEDGE_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED),
                    commit=False,
                )
                if changed:
                    current = await knowledge_job_crud.get_by_id(db, uid=job.uid, job_id=job.id)
                    if current is not None:
                        target_collection = await finalize_knowledge_migration_terminal_state(
                            db,
                            job=current,
                            error=current.error,
                        )
                await db.commit()
            await cleanup_terminal_target_collection(target_collection)
        except Exception as exc:
            logger.bind(job_id=job.id, error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))


def create_knowledge_job_consumer(*, session_factory: SessionFactory = AsyncSessionLocal) -> KnowledgeJobConsumer:
    executor = create_default_knowledge_job_executor(session_factory=session_factory)
    return KnowledgeJobConsumer(executor, session_factory=session_factory)


__all__ = ["KnowledgeJobConsumer", "create_knowledge_job_consumer", "retry_delay_seconds"]
