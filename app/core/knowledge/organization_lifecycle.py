from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_KNOWLEDGE_ORGANIZATION_FAILED
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.managed import (
    managed_knowledge_item_crud,
    organization_lock_token_for_job,
)
from app.core.i18n import t
from app.models.knowledge_base import (
    KnowledgeBase,
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
_ORGANIZATION_OPERATIONS = {
    KnowledgeJobOperation.AUTO_ORGANIZE,
    KnowledgeJobOperation.MANUAL_ORGANIZE,
}
_ORGANIZATION_CHILD_OPERATIONS = {
    KnowledgeJobOperation.ORGANIZE_MUTATION,
    KnowledgeJobOperation.MANAGED_CREATE,
    KnowledgeJobOperation.MANAGED_UPDATE,
    KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
}

__all__ = [
    "is_organization_related_operation",
    "release_organization_locks_if_complete",
    "record_organization_failure",
    "mark_organization_submission_state",
    "get_organization_parent",
    "rollback_organization_mutation_if_needed",
    "cancel_pending_children",
    "is_organization_lifecycle_job",
    "coordinate_organization_terminal",
    "coordinate_organization_cancel_request",
]


def is_organization_related_operation(operation: Any) -> bool:
    try:
        operation = KnowledgeJobOperation(operation)
    except (TypeError, ValueError):
        return False
    return operation in _ORGANIZATION_OPERATIONS or operation in _ORGANIZATION_CHILD_OPERATIONS


def _is_organization_parent_operation(operation: Any) -> bool:
    try:
        return KnowledgeJobOperation(operation) in _ORGANIZATION_OPERATIONS
    except (TypeError, ValueError):
        return False


def _is_organization_child_operation(operation: Any) -> bool:
    try:
        return KnowledgeJobOperation(operation) in _ORGANIZATION_CHILD_OPERATIONS
    except (TypeError, ValueError):
        return False


def _is_terminal_status(status: Any) -> bool:
    try:
        return KnowledgeJobStatus(status) in _TERMINAL_STATUSES
    except (TypeError, ValueError):
        return False


def _is_failed_or_cancelled(status: Any) -> bool:
    try:
        return KnowledgeJobStatus(status) in {
            KnowledgeJobStatus.FAILED,
            KnowledgeJobStatus.CANCELLED,
        }
    except (TypeError, ValueError):
        return False


def is_organization_lifecycle_job(job: KnowledgeJob) -> bool:
    return _is_organization_parent_operation(job.operation) or (job.parent_job_id is not None and _is_organization_child_operation(job.operation))


def _safe_error_summary(error: str | None) -> str | None:
    if error is None:
        return None

    text = "".join(character if character.isprintable() or character in "\t\r\n" else " " for character in str(error))
    text = " ".join(text.split())
    if not text:
        return None
    return text[:2000]


async def _get_organization_job(
    db: AsyncSession,
    *,
    uid: str,
    organization_job_id: int,
) -> KnowledgeJob | None:
    job = await knowledge_job_crud.get_by_id(db, uid=uid, job_id=organization_job_id)
    if job is None:
        return None
    try:
        operation = KnowledgeJobOperation(job.operation)
    except (TypeError, ValueError):
        return None
    return (
        job
        if operation
        in {
            KnowledgeJobOperation.AUTO_ORGANIZE,
            KnowledgeJobOperation.MANUAL_ORGANIZE,
        }
        else None
    )


async def _clear_pending_reference(
    db: AsyncSession,
    *,
    uid: str,
    job_id: int,
    updated_at: Any,
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


async def get_organization_parent(
    db: AsyncSession,
    *,
    child: KnowledgeJob,
) -> KnowledgeJob | None:
    if child.parent_job_id is None:
        return None
    parent = await knowledge_job_crud.get_by_id(db, uid=child.uid, job_id=child.parent_job_id)
    if parent is None or parent.knowledge_base_id != child.knowledge_base_id or not _is_organization_parent_operation(parent.operation):
        return None
    return parent


async def rollback_organization_mutation_if_needed(
    db: AsyncSession,
    *,
    child: KnowledgeJob,
    updated_at: Any,
) -> None:
    if (
        child.id is None
        or child.parent_job_id is None
        or child.operation
        not in {
            KnowledgeJobOperation.MANAGED_UPDATE,
            KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
        }
        or child.knowledge_id is None
        or child.expected_version is None
    ):
        return

    if await get_organization_parent(db, child=child) is None:
        return

    await managed_knowledge_item_crud.rollback_pending_organization_mutation(
        db,
        uid=child.uid,
        knowledge_base_id=child.knowledge_base_id,
        knowledge_id=child.knowledge_id,
        expected_version=child.expected_version,
        job_id=child.id,
        updated_at=updated_at,
        commit=False,
    )


async def cancel_pending_children(
    db: AsyncSession,
    *,
    uid: str,
    parent_job_id: int,
    updated_at: Any,
) -> None:
    result = await db.execute(
        select(KnowledgeJob)
        .where(
            KnowledgeJob.uid == uid,
            KnowledgeJob.parent_job_id == parent_job_id,
            KnowledgeJob.status.in_(
                [
                    KnowledgeJobStatus.PENDING,
                    KnowledgeJobStatus.RETRY,
                    KnowledgeJobStatus.RUNNING,
                ]
            ),
        )
        .execution_options(populate_existing=True)
    )
    for child in result.scalars().all():
        if child.id is None:
            continue
        if child.status == KnowledgeJobStatus.RUNNING:
            if child.operation == KnowledgeJobOperation.MANAGED_DELETE_CLEANUP:
                continue
            await db.execute(
                update(KnowledgeJob)
                .where(
                    KnowledgeJob.uid == uid,
                    KnowledgeJob.id == child.id,
                    KnowledgeJob.parent_job_id == parent_job_id,
                    KnowledgeJob.status == KnowledgeJobStatus.RUNNING,
                    KnowledgeJob.cancel_requested_at.is_(None),
                )
                .values(cancel_requested_at=updated_at, updated_at=updated_at)
                .execution_options(synchronize_session=False)
            )
            continue
        update_result = await db.execute(
            update(KnowledgeJob)
            .where(
                KnowledgeJob.uid == uid,
                KnowledgeJob.id == child.id,
                KnowledgeJob.parent_job_id == parent_job_id,
                KnowledgeJob.status.in_([KnowledgeJobStatus.PENDING, KnowledgeJobStatus.RETRY]),
            )
            .values(
                status=KnowledgeJobStatus.CANCELLED,
                active_change_key=None,
                locked_by=None,
                lock_until=None,
                finished_at=updated_at,
                updated_at=updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        if (update_result.rowcount or 0) == 1:
            await rollback_organization_mutation_if_needed(
                db,
                child=child,
                updated_at=updated_at,
            )
            await _clear_pending_reference(
                db,
                uid=uid,
                job_id=child.id,
                updated_at=updated_at,
            )


async def _get_knowledge_base(
    db: AsyncSession,
    *,
    uid: str,
    job: KnowledgeJob,
) -> KnowledgeBase | None:
    knowledge_base = await knowledge_base_crud.get(db, job.knowledge_base_id)
    return knowledge_base if knowledge_base is not None and knowledge_base.uid == uid else None


async def _finish_db_operation(db: AsyncSession, *, commit: bool) -> None:
    if commit:
        await db.commit()
    else:
        await db.flush()


def _has_failure(jobs: Iterable[KnowledgeJob]) -> bool:
    return any(_is_failed_or_cancelled(job.status) for job in jobs)


def _failure_summary(error: str | None, jobs: Iterable[KnowledgeJob]) -> str:
    summary = _safe_error_summary(error)
    if summary is not None:
        return summary
    for job in jobs:
        if _is_failed_or_cancelled(job.status):
            summary = _safe_error_summary(job.error)
            if summary is not None:
                return summary
    return t(ERR_KNOWLEDGE_ORGANIZATION_FAILED)


async def release_organization_locks_if_complete(
    db: AsyncSession,
    *,
    uid: str,
    organization_job_id: int,
    error: str | None = None,
    commit: bool = False,
) -> bool:
    organization_job = await _get_organization_job(
        db,
        uid=uid,
        organization_job_id=organization_job_id,
    )
    if organization_job is None:
        return False
    if not _is_terminal_status(organization_job.status):
        return False

    child_jobs = await knowledge_job_crud.list_children(
        db,
        uid=uid,
        parent_job_id=organization_job_id,
    )
    if any(not _is_terminal_status(job.status) for job in child_jobs):
        return False

    knowledge_base = await _get_knowledge_base(
        db,
        uid=uid,
        job=organization_job,
    )
    if knowledge_base is None:
        return False

    database_time = await get_database_time(db)
    await managed_knowledge_item_crud.clear_organization_locks(
        db,
        uid=uid,
        organization_lock_token=organization_lock_token_for_job(organization_job_id),
        updated_at=database_time,
        commit=False,
    )

    all_jobs = [organization_job, *child_jobs]
    knowledge_base.organization_error = _failure_summary(error, all_jobs) if _has_failure(all_jobs) else None
    knowledge_base.updated_at = database_time

    await _finish_db_operation(db, commit=commit)
    return True


async def record_organization_failure(
    db: AsyncSession,
    *,
    uid: str,
    organization_job_id: int,
    error: str,
    commit: bool = False,
) -> bool:
    organization_job = await _get_organization_job(
        db,
        uid=uid,
        organization_job_id=organization_job_id,
    )
    if organization_job is None:
        return False

    knowledge_base = await _get_knowledge_base(
        db,
        uid=uid,
        job=organization_job,
    )
    if knowledge_base is None:
        return False

    knowledge_base.organization_error = _safe_error_summary(error) or t(ERR_KNOWLEDGE_ORGANIZATION_FAILED)
    knowledge_base.updated_at = await get_database_time(db)
    await _finish_db_operation(db, commit=commit)
    return True


async def mark_organization_submission_state(
    db: AsyncSession,
    *,
    uid: str,
    organization_job_id: int,
    error: str | None = None,
    commit: bool = False,
) -> bool:
    organization_job = await _get_organization_job(
        db,
        uid=uid,
        organization_job_id=organization_job_id,
    )
    if organization_job is None:
        return False
    if organization_job.status != KnowledgeJobStatus.SUCCEEDED:
        return False

    child_jobs = await knowledge_job_crud.list_children(
        db,
        uid=uid,
        parent_job_id=organization_job_id,
    )
    if any(not _is_terminal_status(job.status) for job in child_jobs):
        return False

    knowledge_base = await _get_knowledge_base(
        db,
        uid=uid,
        job=organization_job,
    )
    if knowledge_base is None:
        return False

    database_time = await get_database_time(db)
    all_jobs = [organization_job, *child_jobs]
    error_summary = _safe_error_summary(error)
    knowledge_base.organization_last_job_id = organization_job_id
    knowledge_base.organization_last_run_at = database_time
    knowledge_base.organization_error = _failure_summary(error, all_jobs) if _has_failure(all_jobs) or error_summary is not None else None
    knowledge_base.updated_at = database_time

    await _finish_db_operation(db, commit=commit)
    return True


async def coordinate_organization_terminal(
    db: AsyncSession,
    uid: str,
    job_id: int,
    error: str | None = None,
    updated_at: Any | None = None,
    commit: bool = False,
) -> bool:
    job = await knowledge_job_crud.get_by_id(db, uid=uid, job_id=job_id)
    if job is None or job.id is None or not is_organization_lifecycle_job(job) or not _is_terminal_status(job.status):
        return False

    is_parent = _is_organization_parent_operation(job.operation)
    if is_parent:
        organization_job_id = job.id
    else:
        organization_parent = await get_organization_parent(db, child=job)
        if organization_parent is None or organization_parent.id is None:
            return False
        organization_job_id = organization_parent.id

    if updated_at is None:
        updated_at = await get_database_time(db)

    if not is_parent and _is_failed_or_cancelled(job.status):
        await rollback_organization_mutation_if_needed(
            db,
            child=job,
            updated_at=updated_at,
        )
        await _clear_pending_reference(
            db,
            uid=uid,
            job_id=job.id,
            updated_at=updated_at,
        )

    if _is_failed_or_cancelled(job.status):
        await record_organization_failure(
            db,
            uid=uid,
            organization_job_id=organization_job_id,
            error=error or t(ERR_KNOWLEDGE_ORGANIZATION_FAILED),
            commit=False,
        )
        if is_parent:
            await cancel_pending_children(
                db,
                uid=uid,
                parent_job_id=job.id,
                updated_at=updated_at,
            )

    released = await release_organization_locks_if_complete(
        db,
        uid=uid,
        organization_job_id=organization_job_id,
        error=error,
        commit=False,
    )
    if released:
        await mark_organization_submission_state(
            db,
            uid=uid,
            organization_job_id=organization_job_id,
            error=error,
            commit=False,
        )

    await _finish_db_operation(db, commit=commit)
    return True


async def coordinate_organization_cancel_request(
    db: AsyncSession,
    uid: str,
    job_id: int,
    commit: bool = False,
) -> bool:
    job = await knowledge_job_crud.get_by_id(db, uid=uid, job_id=job_id)
    if job is None or job.id is None or not _is_organization_parent_operation(job.operation) or job.status != KnowledgeJobStatus.RUNNING or job.cancel_requested_at is None:
        return False

    updated_at = await get_database_time(db)
    await cancel_pending_children(
        db,
        uid=uid,
        parent_job_id=job.id,
        updated_at=updated_at,
    )
    await _finish_db_operation(db, commit=commit)
    return True
