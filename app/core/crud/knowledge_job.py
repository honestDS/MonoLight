from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlmodel import select

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_FIELD_REQUIRED,
    ERR_KNOWLEDGE_JOB_OWNER_MISMATCH,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.i18n import t
from app.models.knowledge_base import (
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeItem,
)
from app.providers.database.time import get_database_time

_TERMINAL_STATUSES = {
    KnowledgeJobStatus.SUCCEEDED,
    KnowledgeJobStatus.FAILED,
    KnowledgeJobStatus.CANCELLED,
}
_SYSTEM_CLEANUP_OPERATIONS = {
    KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
    KnowledgeJobOperation.MANAGED_VECTOR_CLEANUP,
}


def is_system_cleanup_operation(operation: KnowledgeJobOperation | str) -> bool:
    try:
        return KnowledgeJobOperation(operation) in _SYSTEM_CLEANUP_OPERATIONS
    except (TypeError, ValueError):
        return False


def _claim_dependency_condition():
    parent_job = aliased(KnowledgeJob)
    cleanup_parent_terminal = and_(
        KnowledgeJob.operation == KnowledgeJobOperation.MANAGED_VECTOR_CLEANUP,
        KnowledgeJob.parent_job_id.is_not(None),
        select(parent_job.id)
        .where(
            parent_job.id == KnowledgeJob.parent_job_id,
            parent_job.uid == KnowledgeJob.uid,
            parent_job.status.in_(_TERMINAL_STATUSES),
        )
        .exists(),
    )
    return or_(
        KnowledgeJob.operation != KnowledgeJobOperation.MANAGED_VECTOR_CLEANUP,
        cleanup_parent_terminal,
    )


@dataclass(frozen=True, slots=True)
class KnowledgeJobRecoveryResult:
    retried: int = 0
    failed: int = 0
    cancelled: int = 0


@dataclass(frozen=True, slots=True)
class KnowledgeJobCancelResult:
    job: KnowledgeJob | None
    accepted: bool
    changed: bool


def _resolve_owner(owner: str | None, worker_id: str | None = None) -> str:
    if owner and worker_id and owner != worker_id:
        raise ValueError(t(ERR_KNOWLEDGE_JOB_OWNER_MISMATCH))
    resolved = owner or worker_id
    if not isinstance(resolved, str) or not resolved.strip():
        raise ValueError(t(ERR_KNOWLEDGE_JOB_FIELD_REQUIRED, field="owner"))
    return resolved


class CRUDKnowledgeJob:
    async def get_by_id(self, db: AsyncSession, *, uid: str, job_id: int) -> KnowledgeJob | None:
        result = await db.execute(
            select(KnowledgeJob)
            .where(KnowledgeJob.uid == uid, KnowledgeJob.id == job_id)
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def get_by_dedupe_key(self, db: AsyncSession, *, uid: str, dedupe_key: str) -> KnowledgeJob | None:
        result = await db.execute(
            select(KnowledgeJob)
            .where(KnowledgeJob.uid == uid, KnowledgeJob.dedupe_key == dedupe_key)
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def get_by_active_change_key(
        self,
        db: AsyncSession,
        *,
        uid: str,
        active_change_key: str,
    ) -> KnowledgeJob | None:
        result = await db.execute(
            select(KnowledgeJob)
            .where(
                KnowledgeJob.uid == uid,
                KnowledgeJob.active_change_key == active_change_key,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        commit: bool = True,
        **values: Any,
    ) -> tuple[KnowledgeJob, bool]:
        job = KnowledgeJob.model_validate({"uid": uid, **values})
        try:
            async with db.begin_nested():
                db.add(job)
                await db.flush()
        except IntegrityError:
            existing = await self.get_by_dedupe_key(db, uid=uid, dedupe_key=job.dedupe_key)
            if existing is not None:
                return existing, False
            raise
        if commit:
            await db.commit()
        await db.refresh(job)
        return job, True

    async def delete_unstarted(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        commit: bool = True,
    ) -> bool:
        result = await db.execute(
            delete(KnowledgeJob).where(
                KnowledgeJob.uid == uid,
                KnowledgeJob.id == job_id,
                KnowledgeJob.status == KnowledgeJobStatus.PENDING,
                KnowledgeJob.locked_by.is_(None),
            )
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def set_target(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        knowledge_id: int,
        expected_version: int,
        commit: bool = True,
    ) -> KnowledgeJob | None:
        result = await db.execute(
            update(KnowledgeJob)
            .where(
                KnowledgeJob.uid == uid,
                KnowledgeJob.id == job_id,
                KnowledgeJob.status == KnowledgeJobStatus.PENDING,
                KnowledgeJob.locked_by.is_(None),
            )
            .values(
                knowledge_id=knowledge_id,
                expected_version=expected_version,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        if commit:
            await db.commit()
        else:
            await db.flush()
        return await self.get_by_id(db, uid=uid, job_id=job_id)

    async def list_claimable_for_worker(
        self,
        db: AsyncSession,
        *,
        enabled_operations: Iterable[KnowledgeJobOperation | str],
        limit: int = 20,
    ) -> list[KnowledgeJob]:
        operations = [KnowledgeJobOperation(operation) for operation in enabled_operations]
        limit = min(max(limit, 0), 100)
        if not operations or limit == 0:
            return []
        now = await get_database_time(db)
        result = await db.execute(
            select(KnowledgeJob)
            .where(
                KnowledgeJob.status.in_([KnowledgeJobStatus.PENDING, KnowledgeJobStatus.RETRY]),
                KnowledgeJob.available_at <= now,
                KnowledgeJob.cancel_requested_at.is_(None),
                KnowledgeJob.operation.in_(operations),
                _claim_dependency_condition(),
            )
            .order_by(KnowledgeJob.available_at, KnowledgeJob.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def try_claim(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        enabled_operations: Iterable[KnowledgeJobOperation | str] | None = None,
        commit: bool = True,
    ) -> KnowledgeJob | None:
        resolved_owner = _resolve_owner(owner, worker_id)
        if lease_seconds < 1:
            raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="lease_seconds"))
        now = await get_database_time(db)
        conditions: list[Any] = [
            KnowledgeJob.uid == uid,
            KnowledgeJob.id == job_id,
            KnowledgeJob.status.in_([KnowledgeJobStatus.PENDING, KnowledgeJobStatus.RETRY]),
            KnowledgeJob.available_at <= now,
            KnowledgeJob.cancel_requested_at.is_(None),
            _claim_dependency_condition(),
        ]
        if enabled_operations is not None:
            operations = [KnowledgeJobOperation(operation) for operation in enabled_operations]
            if not operations:
                return None
            conditions.append(KnowledgeJob.operation.in_(operations))
        result = await db.execute(
            update(KnowledgeJob)
            .where(*conditions)
            .values(
                status=KnowledgeJobStatus.RUNNING,
                locked_by=resolved_owner,
                lock_until=now + timedelta(seconds=lease_seconds),
                attempt_count=KnowledgeJob.attempt_count + 1,
                started_at=now,
                error=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        if (result.rowcount or 0) != 1:
            return None
        return await self.get_by_id(db, uid=uid, job_id=job_id)

    async def get_active_claim(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
    ) -> KnowledgeJob | None:
        resolved_owner = _resolve_owner(owner, worker_id)
        now = await get_database_time(db)
        result = await db.execute(
            select(KnowledgeJob)
            .where(
                KnowledgeJob.uid == uid,
                KnowledgeJob.id == job_id,
                KnowledgeJob.status == KnowledgeJobStatus.RUNNING,
                KnowledgeJob.locked_by == resolved_owner,
                KnowledgeJob.lock_until >= now,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def renew_lease(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        commit: bool = True,
    ) -> bool:
        resolved_owner = _resolve_owner(owner, worker_id)
        if lease_seconds < 1:
            raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="lease_seconds"))
        now = await get_database_time(db)
        result = await db.execute(
            update(KnowledgeJob)
            .where(
                KnowledgeJob.uid == uid,
                KnowledgeJob.id == job_id,
                KnowledgeJob.status == KnowledgeJobStatus.RUNNING,
                KnowledgeJob.locked_by == resolved_owner,
                KnowledgeJob.lock_until >= now,
                KnowledgeJob.cancel_requested_at.is_(None),
            )
            .values(lock_until=now + timedelta(seconds=lease_seconds), updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def _clear_pending_reference(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        updated_at,
    ) -> None:
        await db.execute(
            update(ManagedKnowledgeItem)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.pending_job_id == job_id,
            )
            .values(pending_job_id=None, updated_at=updated_at)
            .execution_options(synchronize_session=False)
        )

    async def _mark_terminal(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str,
        status: KnowledgeJobStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        commit: bool = True,
    ) -> bool:
        now = await get_database_time(db)
        conditions: list[Any] = [
            KnowledgeJob.uid == uid,
            KnowledgeJob.id == job_id,
            KnowledgeJob.status == KnowledgeJobStatus.RUNNING,
            KnowledgeJob.locked_by == owner,
            KnowledgeJob.lock_until >= now,
        ]
        if status != KnowledgeJobStatus.CANCELLED:
            conditions.append(KnowledgeJob.cancel_requested_at.is_(None))
        update_result = await db.execute(
            update(KnowledgeJob)
            .where(*conditions)
            .values(
                status=status,
                result=result,
                error=error,
                active_change_key=None,
                locked_by=None,
                lock_until=None,
                finished_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        changed = (update_result.rowcount or 0) == 1
        if changed and status in {KnowledgeJobStatus.FAILED, KnowledgeJobStatus.CANCELLED}:
            await self._clear_pending_reference(db, uid=uid, job_id=job_id, updated_at=now)
        if commit:
            await db.commit()
        else:
            await db.flush()
        return changed

    async def mark_succeeded(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str,
        result: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> bool:
        return await self._mark_terminal(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            status=KnowledgeJobStatus.SUCCEEDED,
            result=result,
            commit=commit,
        )

    async def mark_failed(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str,
        error: str,
        result: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> bool:
        return await self._mark_terminal(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            status=KnowledgeJobStatus.FAILED,
            result=result,
            error=error,
            commit=commit,
        )

    async def mark_cancelled(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str,
        error: str | None = None,
        commit: bool = True,
    ) -> bool:
        return await self._mark_terminal(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            status=KnowledgeJobStatus.CANCELLED,
            error=error,
            commit=commit,
        )

    async def release_for_retry(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str,
        error: str,
        delay_seconds: int,
        commit: bool = True,
    ) -> bool:
        now = await get_database_time(db)
        result = await db.execute(
            update(KnowledgeJob)
            .where(
                KnowledgeJob.uid == uid,
                KnowledgeJob.id == job_id,
                KnowledgeJob.status == KnowledgeJobStatus.RUNNING,
                KnowledgeJob.locked_by == owner,
                KnowledgeJob.lock_until >= now,
                KnowledgeJob.cancel_requested_at.is_(None),
            )
            .values(
                status=KnowledgeJobStatus.RETRY,
                error=error,
                available_at=now + timedelta(seconds=max(delay_seconds, 0)),
                locked_by=None,
                lock_until=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def recover_expired(
        self,
        db: AsyncSession,
        *,
        delay_seconds: int = 0,
        max_attempts_error: str | None = None,
        commit: bool = True,
    ) -> KnowledgeJobRecoveryResult:
        now = await get_database_time(db)
        result = await db.execute(
            select(KnowledgeJob).where(
                KnowledgeJob.status == KnowledgeJobStatus.RUNNING,
                KnowledgeJob.lock_until < now,
            )
        )
        retried = failed = cancelled = 0
        for job in list(result.scalars().all()):
            if job.id is None or not job.locked_by or job.lock_until is None:
                continue
            system_cleanup = is_system_cleanup_operation(job.operation)
            if system_cleanup:
                next_status = KnowledgeJobStatus.RETRY
            elif job.cancel_requested_at is not None:
                next_status = KnowledgeJobStatus.CANCELLED
            elif job.attempt_count >= job.max_attempts:
                next_status = KnowledgeJobStatus.FAILED
            else:
                next_status = KnowledgeJobStatus.RETRY
            values: dict[str, Any] = {
                "status": next_status,
                "locked_by": None,
                "lock_until": None,
                "updated_at": now,
            }
            if next_status == KnowledgeJobStatus.RETRY:
                values["available_at"] = now + timedelta(seconds=max(delay_seconds, 0))
                if system_cleanup:
                    values["cancel_requested_at"] = None
            else:
                values["active_change_key"] = None
                values["finished_at"] = now
                if next_status == KnowledgeJobStatus.FAILED and max_attempts_error is not None:
                    values["error"] = max_attempts_error
            update_result = await db.execute(
                update(KnowledgeJob)
                .where(
                    KnowledgeJob.id == job.id,
                    KnowledgeJob.uid == job.uid,
                    KnowledgeJob.status == KnowledgeJobStatus.RUNNING,
                    KnowledgeJob.locked_by == job.locked_by,
                    KnowledgeJob.lock_until == job.lock_until,
                    *(
                        ()
                        if next_status == KnowledgeJobStatus.CANCELLED or system_cleanup
                        else (KnowledgeJob.cancel_requested_at.is_(None),)
                    ),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if (update_result.rowcount or 0) != 1:
                continue
            if next_status in _TERMINAL_STATUSES:
                await self._clear_pending_reference(db, uid=job.uid, job_id=job.id, updated_at=now)
            if next_status == KnowledgeJobStatus.RETRY:
                retried += 1
            elif next_status == KnowledgeJobStatus.FAILED:
                failed += 1
            else:
                cancelled += 1
        if commit:
            await db.commit()
        else:
            await db.flush()
        return KnowledgeJobRecoveryResult(retried=retried, failed=failed, cancelled=cancelled)

    async def request_cancel(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        commit: bool = True,
    ) -> KnowledgeJobCancelResult:
        job = await self.get_by_id(db, uid=uid, job_id=job_id)
        if job is None:
            return KnowledgeJobCancelResult(job=None, accepted=False, changed=False)
        if is_system_cleanup_operation(job.operation) or job.status in _TERMINAL_STATUSES:
            return KnowledgeJobCancelResult(job=job, accepted=False, changed=False)

        now = await get_database_time(db)
        result = await db.execute(
            update(KnowledgeJob)
            .where(
                KnowledgeJob.uid == uid,
                KnowledgeJob.id == job_id,
                KnowledgeJob.status.in_([KnowledgeJobStatus.PENDING, KnowledgeJobStatus.RETRY]),
            )
            .values(
                status=KnowledgeJobStatus.CANCELLED,
                active_change_key=None,
                cancel_requested_at=now,
                locked_by=None,
                lock_until=None,
                finished_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) == 1:
            await self._clear_pending_reference(db, uid=uid, job_id=job_id, updated_at=now)
            if commit:
                await db.commit()
            else:
                await db.flush()
            return KnowledgeJobCancelResult(
                job=await self.get_by_id(db, uid=uid, job_id=job_id),
                accepted=True,
                changed=True,
            )

        job = await self.get_by_id(db, uid=uid, job_id=job_id)
        if job is None:
            return KnowledgeJobCancelResult(job=None, accepted=False, changed=False)
        if is_system_cleanup_operation(job.operation) or job.status in _TERMINAL_STATUSES:
            return KnowledgeJobCancelResult(job=job, accepted=False, changed=False)
        if job.status != KnowledgeJobStatus.RUNNING:
            return KnowledgeJobCancelResult(job=job, accepted=False, changed=False)
        if job.cancel_requested_at is not None:
            return KnowledgeJobCancelResult(job=job, accepted=True, changed=False)

        now = await get_database_time(db)
        result = await db.execute(
            update(KnowledgeJob)
            .where(
                KnowledgeJob.uid == uid,
                KnowledgeJob.id == job_id,
                KnowledgeJob.status == KnowledgeJobStatus.RUNNING,
                KnowledgeJob.locked_by == job.locked_by,
                KnowledgeJob.lock_until == job.lock_until,
                KnowledgeJob.cancel_requested_at.is_(None),
            )
            .values(cancel_requested_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) == 1:
            if commit:
                await db.commit()
            else:
                await db.flush()
            return KnowledgeJobCancelResult(
                job=await self.get_by_id(db, uid=uid, job_id=job_id),
                accepted=True,
                changed=True,
            )

        current = await self.get_by_id(db, uid=uid, job_id=job_id)
        if (
            current is not None
            and current.status == KnowledgeJobStatus.RUNNING
            and current.cancel_requested_at is not None
        ):
            return KnowledgeJobCancelResult(job=current, accepted=True, changed=False)
        return KnowledgeJobCancelResult(job=current, accepted=False, changed=False)

    async def release_claim_for_shutdown(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str,
        delay_seconds: int,
        max_attempts_error: str,
        commit: bool = True,
    ) -> bool:
        job = await self.get_by_id(db, uid=uid, job_id=job_id)
        if job is None or job.status != KnowledgeJobStatus.RUNNING or job.locked_by != owner:
            return False
        if is_system_cleanup_operation(job.operation):
            now = await get_database_time(db)
            result = await db.execute(
                update(KnowledgeJob)
                .where(
                    KnowledgeJob.uid == uid,
                    KnowledgeJob.id == job_id,
                    KnowledgeJob.status == KnowledgeJobStatus.RUNNING,
                    KnowledgeJob.locked_by == owner,
                )
                .values(
                    status=KnowledgeJobStatus.RETRY,
                    available_at=now + timedelta(seconds=max(delay_seconds, 0)),
                    cancel_requested_at=None,
                    locked_by=None,
                    lock_until=None,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if commit:
                await db.commit()
            else:
                await db.flush()
            return (result.rowcount or 0) == 1
        if job.cancel_requested_at is not None:
            return await self.mark_cancelled(
                db,
                uid=uid,
                job_id=job_id,
                owner=owner,
                commit=commit,
            )
        if job.attempt_count >= job.max_attempts:
            return await self.mark_failed(
                db,
                uid=uid,
                job_id=job_id,
                owner=owner,
                error=max_attempts_error,
                commit=commit,
            )
        now = await get_database_time(db)
        result = await db.execute(
            update(KnowledgeJob)
            .where(
                KnowledgeJob.uid == uid,
                KnowledgeJob.id == job_id,
                KnowledgeJob.status == KnowledgeJobStatus.RUNNING,
                KnowledgeJob.locked_by == owner,
                KnowledgeJob.cancel_requested_at.is_(None),
            )
            .values(
                status=KnowledgeJobStatus.RETRY,
                available_at=now + timedelta(seconds=max(delay_seconds, 0)),
                locked_by=None,
                lock_until=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1


knowledge_job_crud = CRUDKnowledgeJob()


__all__ = [
    "KnowledgeJobCancelResult",
    "KnowledgeJobRecoveryResult",
    "is_system_cleanup_operation",
    "knowledge_job_crud",
]
