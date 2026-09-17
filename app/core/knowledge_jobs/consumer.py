from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from math import isfinite
from numbers import Real

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_CANCELLATION_REQUESTED,
    ERR_KNOWLEDGE_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED,
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_RENEW_INTERVAL_INVALID,
    ERR_KNOWLEDGE_JOB_UNEXPECTED_FAILURE,
    ERR_KNOWLEDGE_ORGANIZATION_FAILED,
    ERR_VALUE_MUST_BE_POSITIVE,
    KNOWLEDGE_COLLECTION_CLEANUP_BATCH_LIMIT,
    KNOWLEDGE_COLLECTION_CLEANUP_INTERVAL_SECONDS,
    KNOWLEDGE_JOB_SYSTEM_CLEANUP_RETRY_DELAY_SECONDS,
    LOG_KNOWLEDGE_JOB_CANCELLED,
    LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED,
    LOG_KNOWLEDGE_JOB_EXECUTION_FAILED,
    LOG_KNOWLEDGE_JOB_LEASE_LOST,
    LOG_KNOWLEDGE_JOB_LOOP_FAILED,
    LOG_KNOWLEDGE_JOB_STARTUP_RECOVERY_COMPLETED,
    LOG_KNOWLEDGE_JOB_STATE_UPDATE_FAILED,
    LOG_KNOWLEDGE_ORGANIZATION_COMPLETED,
    LOG_KNOWLEDGE_ORGANIZATION_STARTED,
    LOG_MANAGED_MEMORY_KB_MIGRATION_FAILED,
    LOG_MANAGED_MEMORY_KB_MIGRATION_RETRY,
    MANAGED_MEMORY_KB_MIGRATION_DEDUPE_PREFIX,
    MANAGED_MEMORY_KB_MIGRATION_RETRY_DELAY_SECONDS,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.job import (
    KnowledgeJobRecoveryResult,
    is_system_cleanup_operation,
    knowledge_job_crud,
)
from app.core.i18n import t
from app.core.knowledge.organization_lifecycle import coordinate_organization_terminal
from app.core.knowledge_base_collection_cleanup import process_pending_collection_cleanups
from app.core.knowledge_jobs.executor import (
    KnowledgeJobCancelledError,
    KnowledgeJobDeterministicError,
    KnowledgeJobExecutionError,
    KnowledgeJobExecutionResult,
    KnowledgeJobExecutor,
    KnowledgeJobLeaseLostError,
    KnowledgeJobRetryableError,
    SessionFactory,
)
from app.core.knowledge_jobs.handlers import create_default_knowledge_job_executor
from app.core.knowledge_jobs.maintenance import submit_startup_knowledge_maintenance_jobs
from app.core.knowledge_jobs.migration import finalize_knowledge_migration_terminal_state
from app.core.log import get_logger
from app.models.knowledge_base import KnowledgeJob, KnowledgeJobOperation, KnowledgeJobStatus
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

_ORGANIZATION_PARENT_OPERATIONS = frozenset(
    {
        KnowledgeJobOperation.AUTO_ORGANIZE,
        KnowledgeJobOperation.MANUAL_ORGANIZE,
    }
)
_TERMINAL_KNOWLEDGE_JOB_STATUSES = frozenset(
    {
        KnowledgeJobStatus.SUCCEEDED,
        KnowledgeJobStatus.FAILED,
        KnowledgeJobStatus.CANCELLED,
    }
)
_FAILED_KNOWLEDGE_JOB_STATUSES = frozenset(
    {
        KnowledgeJobStatus.FAILED,
        KnowledgeJobStatus.CANCELLED,
    }
)


def retry_delay_seconds(attempt_count: int) -> int:
    if attempt_count <= 1:
        return 1
    if attempt_count >= 10:
        return KNOWLEDGE_JOB_RETRY_MAX_SECONDS
    return min(KNOWLEDGE_JOB_RETRY_MAX_SECONDS, 2 ** (attempt_count - 1))


def _is_managed_memory_embedding_migration(job: KnowledgeJob) -> bool:
    return job.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION and job.dedupe_key.startswith(f"{MANAGED_MEMORY_KB_MIGRATION_DEDUPE_PREFIX}:")


def _is_organization_parent_job(job: KnowledgeJob) -> bool:
    return job.operation in _ORGANIZATION_PARENT_OPERATIONS


def _organization_result_count(result: dict | None, field: str) -> int:
    if not isinstance(result, dict):
        return 0
    value = result.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


async def _organization_knowledge_base_name(session_factory: SessionFactory, job: KnowledgeJob) -> str:
    try:
        async with session_factory() as db:
            knowledge_base = await knowledge_base_crud.get(db, job.knowledge_base_id)
        if knowledge_base is not None and knowledge_base.uid == job.uid:
            return knowledge_base.name
    except Exception:
        pass
    return f"#{job.knowledge_base_id}"


async def _log_organization_started(session_factory: SessionFactory, job: KnowledgeJob) -> None:
    if not _is_organization_parent_job(job) or job.id is None:
        return
    knowledge_base_name = await _organization_knowledge_base_name(session_factory, job)
    operation = job.operation.value if hasattr(job.operation, "value") else str(job.operation)
    logger.bind(
        uid=job.uid,
        knowledge_base_id=job.knowledge_base_id,
        knowledge_base_name=knowledge_base_name,
        job_id=job.id,
        operation=operation,
    ).info(
        t(
            LOG_KNOWLEDGE_ORGANIZATION_STARTED,
            knowledge_base_name=knowledge_base_name,
            knowledge_base_id=job.knowledge_base_id,
            job_id=job.id,
            operation=operation,
        )
    )


async def _log_organization_completed(session_factory: SessionFactory, job: KnowledgeJob, result: dict | None) -> None:
    if not _is_organization_parent_job(job) or job.id is None:
        return
    knowledge_base_name = await _organization_knowledge_base_name(session_factory, job)
    mutation_job_ids = result.get("mutation_job_ids") if isinstance(result, dict) else None
    mutation_job_count = len(mutation_job_ids) if isinstance(mutation_job_ids, list) else 0
    counts = {
        "keep_count": _organization_result_count(result, "keep_count"),
        "update_count": _organization_result_count(result, "update_count"),
        "merge_count": _organization_result_count(result, "merge_count"),
        "conflict_count": _organization_result_count(result, "conflict_count"),
    }
    logger.bind(
        uid=job.uid,
        knowledge_base_id=job.knowledge_base_id,
        knowledge_base_name=knowledge_base_name,
        job_id=job.id,
        mutation_job_count=mutation_job_count,
        **counts,
    ).info(
        t(
            LOG_KNOWLEDGE_ORGANIZATION_COMPLETED,
            knowledge_base_name=knowledge_base_name,
            knowledge_base_id=job.knowledge_base_id,
            job_id=job.id,
            mutation_job_count=mutation_job_count,
            **counts,
        )
    )


def _retry_delay_seconds_for_job(job: KnowledgeJob) -> int:
    if _is_managed_memory_embedding_migration(job):
        return MANAGED_MEMORY_KB_MIGRATION_RETRY_DELAY_SECONDS
    return retry_delay_seconds(job.attempt_count)


def _log_managed_memory_migration_retry(job: KnowledgeJob) -> None:
    if not _is_managed_memory_embedding_migration(job):
        return
    logger.bind(
        job_id=job.id,
        knowledge_base_id=job.knowledge_base_id,
        attempt=job.attempt_count,
        max_attempts=job.max_attempts,
    ).warning(
        t(
            LOG_MANAGED_MEMORY_KB_MIGRATION_RETRY,
            attempt=job.attempt_count,
            max_attempts=job.max_attempts,
        )
    )


def _log_managed_memory_migration_failed(job: KnowledgeJob) -> None:
    if not _is_managed_memory_embedding_migration(job):
        return
    logger.bind(
        job_id=job.id,
        knowledge_base_id=job.knowledge_base_id,
        attempt=job.attempt_count,
        max_attempts=job.max_attempts,
    ).error(
        t(
            LOG_MANAGED_MEMORY_KB_MIGRATION_FAILED,
            attempt=job.attempt_count,
            max_attempts=job.max_attempts,
        )
    )


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
    waiting_for_children: bool = False


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
        "_last_collection_cleanup_at",
        "_startup_maintenance_submitted",
        "_collection_cleanup_interval_seconds",
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
        collection_cleanup_interval_seconds: Real = KNOWLEDGE_COLLECTION_CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        self._poll_interval_seconds = _positive_number(poll_interval_seconds, field="poll_interval_seconds")
        self._lease_seconds = _positive_number(lease_seconds, field="lease_seconds")
        self._renew_interval_seconds = _positive_number(renew_interval_seconds, field="renew_interval_seconds")
        self._recovery_interval_seconds = _positive_number(recovery_interval_seconds, field="recovery_interval_seconds")
        self._max_concurrency = _positive_integer(max_concurrency, field="max_concurrency")
        self._recovery_retry_delay_seconds = _positive_number(recovery_retry_delay_seconds, field="recovery_retry_delay_seconds")
        self._shutdown_retry_delay_seconds = _positive_number(shutdown_retry_delay_seconds, field="shutdown_retry_delay_seconds")
        self._collection_cleanup_interval_seconds = _positive_number(collection_cleanup_interval_seconds, field="collection_cleanup_interval_seconds")
        if self._renew_interval_seconds >= self._lease_seconds:
            raise ValueError(t(ERR_KNOWLEDGE_JOB_RENEW_INTERVAL_INVALID))
        self._executor = executor
        self._session_factory = session_factory
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._running: dict[int, _RunningJob] = {}
        self._last_recovery_at = 0.0
        self._last_lease_renewal_at = 0.0
        self._last_collection_cleanup_at = 0.0
        self._startup_maintenance_submitted = False

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._last_recovery_at = 0.0
        self._last_lease_renewal_at = 0.0
        self._last_collection_cleanup_at = 0.0
        self._startup_maintenance_submitted = False
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
            self._startup_maintenance_submitted = await self._submit_startup_maintenance_jobs()
            await self._process_orphan_collection_cleanups()
            self._last_collection_cleanup_at = loop.time()
            while not self._stop_event.is_set():
                try:
                    now = loop.time()
                    if now - self._last_recovery_at >= self._recovery_interval_seconds:
                        await self._recover_expired()
                        self._last_recovery_at = now
                    if now - self._last_lease_renewal_at >= self._renew_interval_seconds:
                        await self._renew_running()
                        self._last_lease_renewal_at = now
                    if not self._startup_maintenance_submitted:
                        self._startup_maintenance_submitted = await self._submit_startup_maintenance_jobs()
                    if now - self._last_collection_cleanup_at >= self._collection_cleanup_interval_seconds:
                        await self._process_orphan_collection_cleanups()
                        self._last_collection_cleanup_at = now
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

    async def _submit_startup_maintenance_jobs(self) -> bool:
        if KnowledgeJobOperation.KNOWLEDGE_MAINTENANCE not in self._executor.enabled_operations:
            return True
        try:
            async with self._session_factory() as db:
                await submit_startup_knowledge_maintenance_jobs(db)
                await db.commit()
            return True
        except Exception as exc:
            logger.bind(error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))
            return False

    async def _process_orphan_collection_cleanups(self) -> None:
        try:
            async with self._session_factory() as db:
                await process_pending_collection_cleanups(
                    db,
                    limit=KNOWLEDGE_COLLECTION_CLEANUP_BATCH_LIMIT,
                )
        except Exception as exc:
            logger.bind(error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))

    async def _recover_expired(self) -> KnowledgeJobRecoveryResult | None:
        try:
            async with self._session_factory() as db:
                recovery = await knowledge_job_crud.recover_expired(
                    db,
                    delay_seconds=int(self._recovery_retry_delay_seconds),
                    system_cleanup_delay_seconds=KNOWLEDGE_JOB_SYSTEM_CLEANUP_RETRY_DELAY_SECONDS,
                    max_attempts_error=t(ERR_KNOWLEDGE_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED),
                    commit=False,
                )
                for terminal in recovery.terminal_jobs:
                    if terminal.job.id is None:
                        continue
                    await coordinate_organization_terminal(
                        db,
                        uid=terminal.job.uid,
                        job_id=terminal.job.id,
                        error=terminal.error,
                        commit=False,
                    )
                    current = await knowledge_job_crud.get_by_id(
                        db,
                        uid=terminal.job.uid,
                        job_id=terminal.job.id,
                    )
                    if current is None:
                        continue
                    await finalize_knowledge_migration_terminal_state(
                        db,
                        job=current,
                        error=terminal.error,
                    )
                await db.commit()
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
                entry.task.cancel()
                continue
            if not renewed:
                entry.task.cancel()

    async def _claim_candidates(self, candidates: list[KnowledgeJob], capacity: int) -> int:
        claimed_count = 0
        for candidate in candidates:
            if claimed_count >= capacity:
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
            claimed_count += 1
        return claimed_count

    async def _claim_available(self) -> None:
        if not self._executor.enabled_operations:
            return

        normal_capacity = max(0, self._max_concurrency - len(self._running))
        if normal_capacity > 0:
            async with self._session_factory() as db:
                candidates = await knowledge_job_crud.list_claimable_for_worker(
                    db,
                    enabled_operations=self._executor.enabled_operations,
                    limit=max(normal_capacity * 2, normal_capacity),
                )
            await self._claim_candidates(candidates, normal_capacity)

        waiting_parent_ids = {job_id for job_id, entry in self._running.items() if entry.waiting_for_children and not entry.task.done()}
        if not waiting_parent_ids:
            return

        active_execution_count = sum(1 for entry in self._running.values() if not entry.task.done() and not entry.waiting_for_children)
        dependent_capacity = self._max_concurrency - active_execution_count
        if dependent_capacity <= 0:
            return
        async with self._session_factory() as db:
            candidates = await knowledge_job_crud.list_claimable_for_worker(
                db,
                enabled_operations=self._executor.enabled_operations,
                parent_job_ids=waiting_parent_ids,
                limit=max(dependent_capacity * 2, dependent_capacity),
            )
        await self._claim_candidates(candidates, dependent_capacity)

    def _set_waiting_for_children(self, job_id: int, worker_id: str, waiting: bool) -> None:
        entry = self._running.get(job_id)
        if entry is not None and entry.worker_id == worker_id:
            entry.waiting_for_children = waiting

    async def _execute(self, job: KnowledgeJob, worker_id: str) -> None:
        if job.id is None:
            return
        try:
            await _log_organization_started(self._session_factory, job)
            execution = await self._executor.execute_claimed(job, worker_id)
            if execution.finalized:
                await self._submit_auto_organization_after_publication(job)
                return
            if execution.wait_for_children and _is_organization_parent_job(job):
                self._set_waiting_for_children(job.id, worker_id, True)
                try:
                    await self._wait_for_organization_children(job, worker_id, execution)
                finally:
                    self._set_waiting_for_children(job.id, worker_id, False)
                return
            async with self._session_factory() as db:
                changed = await knowledge_job_crud.mark_succeeded(
                    db,
                    uid=job.uid,
                    job_id=job.id,
                    owner=worker_id,
                    result=execution.result,
                    commit=False,
                )
                if changed:
                    await coordinate_organization_terminal(
                        db,
                        uid=job.uid,
                        job_id=job.id,
                        commit=False,
                    )
                await db.commit()
            if not changed:
                logger.bind(job_id=job.id, operation=str(job.operation)).warning(t(LOG_KNOWLEDGE_JOB_STATE_UPDATE_FAILED))
            else:
                await _log_organization_completed(self._session_factory, job, execution.result)
                await self._submit_auto_organization_after_publication(job)
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

    async def _wait_for_organization_children(
        self,
        job: KnowledgeJob,
        worker_id: str,
        execution: KnowledgeJobExecutionResult,
    ) -> None:
        if job.id is None:
            return
        while True:
            completed_successfully = False
            async with self._session_factory() as db:
                active_claim = await knowledge_job_crud.get_active_claim(
                    db,
                    uid=job.uid,
                    job_id=job.id,
                    owner=worker_id,
                )
                children = await knowledge_job_crud.list_children(
                    db,
                    uid=job.uid,
                    parent_job_id=job.id,
                )
                if active_claim is None:
                    raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))

                changed: bool | None = None
                if active_claim.cancel_requested_at is not None:
                    error = t(ERR_KNOWLEDGE_JOB_CANCELLATION_REQUESTED)
                    changed = await knowledge_job_crud.mark_cancelled(
                        db,
                        uid=job.uid,
                        job_id=job.id,
                        owner=worker_id,
                        error=error,
                        commit=False,
                    )
                    if changed:
                        await coordinate_organization_terminal(
                            db,
                            uid=job.uid,
                            job_id=job.id,
                            error=error,
                            commit=False,
                        )
                else:
                    failed_children = [child for child in children if child.status in _FAILED_KNOWLEDGE_JOB_STATUSES]
                    if not children or failed_children:
                        error = next((child.error for child in failed_children if child.error), None) or t(ERR_KNOWLEDGE_ORGANIZATION_FAILED)
                        changed = await knowledge_job_crud.mark_failed(
                            db,
                            uid=job.uid,
                            job_id=job.id,
                            owner=worker_id,
                            error=error,
                            commit=False,
                        )
                        if changed:
                            await coordinate_organization_terminal(
                                db,
                                uid=job.uid,
                                job_id=job.id,
                                error=error,
                                commit=False,
                            )
                    elif all(child.status in _TERMINAL_KNOWLEDGE_JOB_STATUSES for child in children):
                        changed = await knowledge_job_crud.mark_succeeded(
                            db,
                            uid=job.uid,
                            job_id=job.id,
                            owner=worker_id,
                            result=execution.result,
                            commit=False,
                        )
                        if changed:
                            completed_successfully = True
                            await coordinate_organization_terminal(
                                db,
                                uid=job.uid,
                                job_id=job.id,
                                commit=False,
                            )

                if changed is None:
                    continue_waiting = True
                else:
                    await db.commit()
                    continue_waiting = False

            if not continue_waiting:
                if not changed:
                    logger.bind(job_id=job.id, operation=str(job.operation)).warning(t(LOG_KNOWLEDGE_JOB_STATE_UPDATE_FAILED))
                elif completed_successfully:
                    await _log_organization_completed(self._session_factory, job, execution.result)
                return
            await asyncio.sleep(float(self._poll_interval_seconds))

    async def _submit_auto_organization_after_publication(self, job: KnowledgeJob) -> None:
        if (
            job.operation
            not in {
                KnowledgeJobOperation.MANAGED_CREATE,
                KnowledgeJobOperation.MANAGED_UPDATE,
            }
            or job.parent_job_id is not None
        ):
            return
        try:
            from app.core.knowledge.organization_run import submit_auto_knowledge_organization

            async with self._session_factory() as db:
                await submit_auto_knowledge_organization(
                    db,
                    uid=job.uid,
                    knowledge_base_id=job.knowledge_base_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.bind(
                job_id=job.id,
                knowledge_base_id=job.knowledge_base_id,
                error_type=type(exc).__name__,
            ).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))

    async def _retry_or_fail(self, job: KnowledgeJob, worker_id: str, safe_message: str, result: dict | None) -> None:
        if job.id is None:
            return
        try:
            if _is_organization_parent_job(job):
                await self._mark_failed(job, worker_id, safe_message, result)
                return
            log_managed_retry = False
            log_managed_failure = False
            async with self._session_factory() as db:
                if is_system_cleanup_operation(job.operation):
                    changed = await knowledge_job_crud.release_for_retry(
                        db,
                        uid=job.uid,
                        job_id=job.id,
                        owner=worker_id,
                        error=safe_message,
                        delay_seconds=KNOWLEDGE_JOB_SYSTEM_CLEANUP_RETRY_DELAY_SECONDS,
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
                        await coordinate_organization_terminal(
                            db,
                            uid=job.uid,
                            job_id=job.id,
                            error=safe_message,
                            commit=False,
                        )
                        current = await knowledge_job_crud.get_by_id(db, uid=job.uid, job_id=job.id)
                        if current is not None:
                            await finalize_knowledge_migration_terminal_state(
                                db,
                                job=current,
                                error=safe_message,
                            )
                    log_managed_failure = changed and _is_managed_memory_embedding_migration(job)
                else:
                    changed = await knowledge_job_crud.release_for_retry(
                        db,
                        uid=job.uid,
                        job_id=job.id,
                        owner=worker_id,
                        error=safe_message,
                        delay_seconds=_retry_delay_seconds_for_job(job),
                        commit=False,
                    )
                    log_managed_retry = changed and _is_managed_memory_embedding_migration(job)
                await db.commit()
            if log_managed_failure:
                _log_managed_memory_migration_failed(job)
            elif log_managed_retry:
                _log_managed_memory_migration_retry(job)
            if not changed:
                logger.bind(job_id=job.id, operation=str(job.operation)).warning(t(LOG_KNOWLEDGE_JOB_STATE_UPDATE_FAILED))
        except Exception as exc:
            logger.bind(job_id=job.id, error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))

    async def _mark_failed(self, job: KnowledgeJob, worker_id: str, safe_message: str, result: dict | None) -> None:
        if job.id is None:
            return
        try:
            log_managed_failure = False
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
                    await coordinate_organization_terminal(
                        db,
                        uid=job.uid,
                        job_id=job.id,
                        error=safe_message,
                        commit=False,
                    )
                    current = await knowledge_job_crud.get_by_id(db, uid=job.uid, job_id=job.id)
                    if current is not None:
                        await finalize_knowledge_migration_terminal_state(
                            db,
                            job=current,
                            error=safe_message,
                        )
                    log_managed_failure = _is_managed_memory_embedding_migration(job)
                await db.commit()
            if log_managed_failure:
                _log_managed_memory_migration_failed(job)
            if not changed:
                logger.bind(job_id=job.id, operation=str(job.operation)).warning(t(LOG_KNOWLEDGE_JOB_STATE_UPDATE_FAILED))
        except Exception as exc:
            logger.bind(job_id=job.id, error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))

    async def _mark_cancelled(self, job: KnowledgeJob, worker_id: str, safe_message: str) -> None:
        if job.id is None:
            return
        try:
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
                    await coordinate_organization_terminal(
                        db,
                        uid=job.uid,
                        job_id=job.id,
                        error=safe_message,
                        commit=False,
                    )
                    current = await knowledge_job_crud.get_by_id(db, uid=job.uid, job_id=job.id)
                    if current is not None:
                        await finalize_knowledge_migration_terminal_state(
                            db,
                            job=current,
                            error=safe_message,
                        )
                await db.commit()
            if changed:
                logger.bind(job_id=job.id, operation=str(job.operation)).info(t(LOG_KNOWLEDGE_JOB_CANCELLED))
        except Exception as exc:
            logger.bind(job_id=job.id, error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))

    async def _release_for_shutdown(self, job: KnowledgeJob, worker_id: str) -> None:
        if job.id is None:
            return
        try:
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
                        if current.status in _TERMINAL_KNOWLEDGE_JOB_STATUSES:
                            await coordinate_organization_terminal(
                                db,
                                uid=job.uid,
                                job_id=job.id,
                                error=current.error,
                                commit=False,
                            )
                        await finalize_knowledge_migration_terminal_state(
                            db,
                            job=current,
                            error=current.error,
                        )
                await db.commit()
        except Exception as exc:
            logger.bind(job_id=job.id, error_type=type(exc).__name__).error(t(LOG_KNOWLEDGE_JOB_DATABASE_OPERATION_FAILED))


def create_knowledge_job_consumer(*, session_factory: SessionFactory = AsyncSessionLocal) -> KnowledgeJobConsumer:
    executor = create_default_knowledge_job_executor(session_factory=session_factory)
    return KnowledgeJobConsumer(executor, session_factory=session_factory)


__all__ = ["KnowledgeJobConsumer", "create_knowledge_job_consumer", "retry_delay_seconds"]
