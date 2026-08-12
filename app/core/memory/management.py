from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED,
    ERR_MEMORY_JOB_NOT_FOUND,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_MAINTENANCE_STATE_CONFLICT,
    ERR_MEMORY_MIGRATION_NOT_FOUND,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_RECORD_NOT_FOUND,
    MEMORY_CONTENT_MAX_TOKENS,
    MEMORY_MAX_ACTIVE_RECORDS,
    MEMORY_ORGANIZE_TRIGGER_RECORDS,
)
from app.core.crud.memory import (
    memory_embedding_revision_crud,
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.memory.errors import MemoryConflictError, MemoryNotFoundError, MemoryValidationError
from app.core.memory.identifiers import build_memory_collection_name
from app.core.memory.maintenance import submit_memory_reindex
from app.core.memory.management_helpers import (
    _ACTIVE_MIGRATION_STATUSES,
    _BLOCKING_CLEANUP_STATUSES,
    _TERMINAL_JOB_STATUSES,
    _active_store_matches,
    _cancel_view,
    _job_view,
    _json_value,
    _migration_status,
    _migration_view,
    _model_view,
    _mutation_view,
    _new_dedupe_key,
    _optional_enum,
    _page,
    _record_view,
    _store_progress_view,
    _submission_view,
    _validate_management_active_store,
    _validated_migration_payload,
)
from app.core.memory.normalization import _normalize_uid, _require_positive, normalize_memory_publication_payload
from app.core.memory.organization import get_organization_settings, update_organization_settings
from app.core.memory.service import memory_service
from app.core.memory_jobs.manager import MemoryJobSubmissionError, memory_job_manager
from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryCapacityStatus,
    LongTermMemoryEmbeddingRevisionStatus,
    LongTermMemoryIndexStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryType,
)


async def list_memories(
    db: AsyncSession,
    *,
    uid: str,
    skip: int = 0,
    limit: int = 100,
    keyword: str | None = None,
    memory_type: LongTermMemoryType | str | None = None,
    sort_by: str | None = None,
    sort_order: str = "desc",
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_skip, normalized_limit = _page(skip, limit)
    normalized_type = _optional_enum(memory_type, LongTermMemoryType, field="memory_type")
    items = await memory_record_crud.get_page(
        db,
        uid=normalized_uid,
        skip=normalized_skip,
        limit=normalized_limit,
        keyword=keyword,
        memory_type=normalized_type,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    total = await memory_record_crud.count(
        db,
        uid=normalized_uid,
        keyword=keyword,
        memory_type=normalized_type,
    )
    return {"items": [_record_view(item) for item in items], "total": total, "skip": normalized_skip, "limit": normalized_limit}


async def get_memory(db: AsyncSession, *, uid: str, memory_id: int) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_memory_id = _require_positive(memory_id, field="memory_id")
    record = await memory_record_crud.get_by_id(db, uid=normalized_uid, memory_id=normalized_memory_id)
    if record is None:
        raise MemoryNotFoundError(ERR_MEMORY_RECORD_NOT_FOUND)
    result = _record_view(record) or {}
    if record.pending_mutation_job_id is not None:
        pending_job = await memory_job_crud.get_by_id(
            db,
            uid=normalized_uid,
            job_id=record.pending_mutation_job_id,
        )
        result["pending_job"] = _job_view(pending_job)
    return result


async def pin_memory(db: AsyncSession, *, uid: str, memory_id: int) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_memory_id = _require_positive(memory_id, field="memory_id")
    record = await memory_service.pin(db, normalized_uid, normalized_memory_id)
    return _record_view(record) or {}


async def unpin_memory(db: AsyncSession, *, uid: str, memory_id: int) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_memory_id = _require_positive(memory_id, field="memory_id")
    record = await memory_service.unpin(db, normalized_uid, normalized_memory_id)
    return _record_view(record) or {}


async def update_memory_settings(
    db: AsyncSession,
    *,
    uid: str,
    auto_organize_enabled: bool,
    organization_channel_id: int | None,
    organization_model_id: str | None,
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    try:
        await update_organization_settings(
            db,
            uid=normalized_uid,
            auto_organize_enabled=auto_organize_enabled,
            organization_channel_id=organization_channel_id,
            organization_model_id=organization_model_id,
        )
        return await get_memory_settings(db, uid=normalized_uid)
    except Exception:
        await db.rollback()
        raise


async def submit_memory_organization(
    db: AsyncSession,
    *,
    uid: str,
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    try:
        submission = await memory_job_manager.submit_organization(
            db,
            uid=normalized_uid,
            dedupe_key=dedupe_key,
        )
        if submission.job.id is None:
            raise MemoryConflictError(ERR_MEMORY_JOB_PAYLOAD_INVALID)
        return {
            "job_id": submission.job.id,
            **_submission_view(submission),
        }
    except Exception:
        await db.rollback()
        raise


async def list_memory_history(
    db: AsyncSession,
    *,
    uid: str,
    memory_id: int,
    skip: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_memory_id = _require_positive(memory_id, field="memory_id")
    normalized_skip, normalized_limit = _page(skip, limit)
    record = await memory_record_crud.get_by_id(db, uid=normalized_uid, memory_id=normalized_memory_id)
    if record is None and not await memory_revision_crud.list_by_memory_id(db, uid=normalized_uid, memory_id=normalized_memory_id, skip=0, limit=1):
        raise MemoryNotFoundError(ERR_MEMORY_RECORD_NOT_FOUND)
    items = await memory_revision_crud.list_by_memory_id(
        db,
        uid=normalized_uid,
        memory_id=normalized_memory_id,
        skip=normalized_skip,
        limit=normalized_limit,
    )
    total = await memory_revision_crud.count_by_memory_id(
        db,
        uid=normalized_uid,
        memory_id=normalized_memory_id,
    )
    return {"items": [_model_view(item) for item in items], "total": total, "skip": normalized_skip, "limit": normalized_limit}


async def list_jobs(
    db: AsyncSession,
    *,
    uid: str,
    skip: int = 0,
    limit: int = 100,
    status: LongTermMemoryMutationStatus | str | None = None,
    operation: LongTermMemoryMutationOperation | str | None = None,
    memory_id: int | None = None,
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_skip, normalized_limit = _page(skip, limit)
    normalized_status = _optional_enum(status, LongTermMemoryMutationStatus, field="status")
    normalized_operation = _optional_enum(operation, LongTermMemoryMutationOperation, field="operation")
    normalized_memory_id = _require_positive(memory_id, field="memory_id") if memory_id is not None else None
    items = await memory_job_crud.get_page(
        db,
        uid=normalized_uid,
        skip=normalized_skip,
        limit=normalized_limit,
        status=normalized_status,
        operation=normalized_operation,
        memory_id=normalized_memory_id,
    )
    total = await memory_job_crud.count(
        db,
        uid=normalized_uid,
        status=normalized_status,
        operation=normalized_operation,
        memory_id=normalized_memory_id,
    )
    return {"items": [_job_view(item) for item in items], "total": total, "skip": normalized_skip, "limit": normalized_limit}


async def get_job(db: AsyncSession, *, uid: str, job_id: int) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_job_id = _require_positive(job_id, field="job_id")
    job = await memory_job_crud.get_by_id(db, uid=normalized_uid, job_id=normalized_job_id)
    if job is None:
        raise MemoryNotFoundError(ERR_MEMORY_JOB_NOT_FOUND)
    return _job_view(job) or {}


async def _get_job_or_raise(db: AsyncSession, *, uid: str, job_id: int, error_key: str = ERR_MEMORY_JOB_NOT_FOUND) -> LongTermMemoryMutationJob:
    job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)
    if job is None:
        raise MemoryNotFoundError(error_key)
    return job


async def _retry_publication_job(
    db: AsyncSession,
    *,
    uid: str,
    job: LongTermMemoryMutationJob,
) -> dict[str, Any]:
    stored_payload = dict(job.payload or {})
    if job.operation == LongTermMemoryMutationOperation.CREATE_WITH_EVICTION:
        publication = stored_payload.get("publication")
        if not isinstance(publication, dict):
            raise MemoryConflictError(ERR_MEMORY_JOB_PAYLOAD_INVALID)
        payload = normalize_memory_publication_payload(dict(publication))
    else:
        payload = normalize_memory_publication_payload(stored_payload)
    dedupe_key = _new_dedupe_key(job)
    common = {
        "uid": uid,
        "dedupe_key": dedupe_key,
        "content": payload["content"],
        "memory_key": payload["memory_key"],
        "memory_type": payload["memory_type"],
        "change_evidence": payload["change_evidence"],
        "source": payload["source"],
        "source_id": payload["source_id"],
        "source_session_id": payload["source_session_id"],
        "source_profile_id": payload["source_profile_id"],
        "source_message_id": payload["source_message_id"],
        "max_attempts": job.max_attempts,
        "commit": False,
    }
    if job.operation in {
        LongTermMemoryMutationOperation.CREATE,
        LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
    }:
        result = await memory_service.create(db, **common)
    elif job.operation == LongTermMemoryMutationOperation.UPDATE:
        if job.memory_id is None or job.expected_version is None:
            raise MemoryConflictError(ERR_MEMORY_JOB_PAYLOAD_INVALID)
        suppress_current = payload.get("suppress_current", False)
        if not isinstance(suppress_current, bool):
            raise MemoryValidationError(ERR_MEMORY_JOB_PAYLOAD_INVALID)
        result = await memory_service.update(
            db,
            **common,
            memory_id=job.memory_id,
            expected_version=job.expected_version,
            suppress_current=suppress_current,
        )
    else:
        raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    if _json_value(result.status) != "accepted" or result.job is None:
        raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    return _mutation_view(result)


async def _retry_organization_with_new_snapshot(
    db: AsyncSession,
    *,
    uid: str,
    job: LongTermMemoryMutationJob,
) -> dict[str, Any]:
    try:
        submission = await memory_job_manager.submit_organization(
            db,
            uid=uid,
            dedupe_key=_new_dedupe_key(job, prefix="memory-organization-retry"),
            commit=False,
        )
    except MemoryJobSubmissionError as exc:
        raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    return {
        "status": "accepted",
        "retry_scope": "new_snapshot",
        **_submission_view(submission),
    }


async def retry_job(db: AsyncSession, *, uid: str, job_id: int) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_job_id = _require_positive(job_id, field="job_id")
    try:
        job = await _get_job_or_raise(db, uid=normalized_uid, job_id=normalized_job_id)
        try:
            status = LongTermMemoryMutationStatus(job.status)
        except (TypeError, ValueError) as exc:
            raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
        if status not in _TERMINAL_JOB_STATUSES:
            raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        operation = LongTermMemoryMutationOperation(job.operation)
        if operation == LongTermMemoryMutationOperation.DELETE_CLEANUP:
            if status != LongTermMemoryMutationStatus.FAILED:
                raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            try:
                submission = await memory_job_manager.retry_delete_cleanup_job(
                    db,
                    failed_job=job,
                    commit=False,
                )
            except MemoryJobSubmissionError as exc:
                raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
            result = {"status": "accepted", **_submission_view(submission)}
        elif operation == LongTermMemoryMutationOperation.EMBEDDING_MIGRATION:
            result = await retry_embedding_migration(db, uid=normalized_uid, migration_id=normalized_job_id, commit=False)
        elif operation == LongTermMemoryMutationOperation.REINDEX:
            submission = await submit_memory_reindex(
                db,
                uid=normalized_uid,
                dedupe_key=_new_dedupe_key(job, prefix="memory-reindex-retry"),
                source_session_id=job.source_session_id,
                source_profile_id=job.source_profile_id,
                source_message_id=job.source_message_id,
                max_attempts=job.max_attempts,
                commit=False,
            )
            result = _submission_view(submission)
        elif operation in {
            LongTermMemoryMutationOperation.CREATE,
            LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            LongTermMemoryMutationOperation.UPDATE,
        }:
            result = await _retry_publication_job(db, uid=normalized_uid, job=job)
        elif operation in {
            LongTermMemoryMutationOperation.ORGANIZE,
            LongTermMemoryMutationOperation.ORGANIZE_MERGE,
        }:
            result = await _retry_organization_with_new_snapshot(db, uid=normalized_uid, job=job)
        else:
            raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        await db.commit()
        return result
    except Exception:
        await db.rollback()
        raise


async def _cancel_loaded_job(db: AsyncSession, *, uid: str, job: LongTermMemoryMutationJob) -> dict[str, Any]:
    cancellation = await memory_job_manager.request_cancel(db, uid=uid, job_id=job.id, commit=False)
    await db.commit()
    return _cancel_view(cancellation)


async def cancel_job(db: AsyncSession, *, uid: str, job_id: int) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_job_id = _require_positive(job_id, field="job_id")
    try:
        job = await _get_job_or_raise(db, uid=normalized_uid, job_id=normalized_job_id)
        return await _cancel_loaded_job(db, uid=normalized_uid, job=job)
    except Exception:
        await db.rollback()
        raise


async def list_embedding_migrations(
    db: AsyncSession,
    *,
    uid: str,
    skip: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_skip, normalized_limit = _page(skip, limit)
    jobs = await memory_job_crud.get_page(
        db,
        uid=normalized_uid,
        skip=normalized_skip,
        limit=normalized_limit,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
    )
    total = await memory_job_crud.count(
        db,
        uid=normalized_uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
    )
    store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
    items: list[dict[str, Any]] = []
    for job in jobs:
        revision = await memory_embedding_revision_crud.get_by_job_id(db, uid=normalized_uid, job_id=job.id)
        items.append(_migration_view(job, revision, store))
    return {"items": items, "total": total, "skip": normalized_skip, "limit": normalized_limit}


async def get_embedding_migration(db: AsyncSession, *, uid: str, migration_id: int) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_migration_id = _require_positive(migration_id, field="migration_id")
    job = await _get_job_or_raise(
        db,
        uid=normalized_uid,
        job_id=normalized_migration_id,
        error_key=ERR_MEMORY_MIGRATION_NOT_FOUND,
    )
    if job.operation != LongTermMemoryMutationOperation.EMBEDDING_MIGRATION:
        raise MemoryNotFoundError(ERR_MEMORY_MIGRATION_NOT_FOUND)
    revision = await memory_embedding_revision_crud.get_by_job_id(
        db,
        uid=normalized_uid,
        job_id=normalized_migration_id,
    )
    store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
    return _migration_view(job, revision, store)


async def retry_embedding_migration(
    db: AsyncSession,
    *,
    uid: str,
    migration_id: int,
    commit: bool = True,
) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_migration_id = _require_positive(migration_id, field="migration_id")
    try:
        old_job = await _get_job_or_raise(
            db,
            uid=normalized_uid,
            job_id=normalized_migration_id,
            error_key=ERR_MEMORY_MIGRATION_NOT_FOUND,
        )
        if old_job.operation != LongTermMemoryMutationOperation.EMBEDDING_MIGRATION:
            raise MemoryNotFoundError(ERR_MEMORY_MIGRATION_NOT_FOUND)
        if LongTermMemoryMutationStatus(old_job.status) not in _TERMINAL_JOB_STATUSES:
            raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        payload = _validated_migration_payload(old_job)
        source = payload["from"]
        target = payload["target"]
        store = await memory_store_crud.lock_for_mutation(db, uid=normalized_uid, commit=False)
        if store is None:
            raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
        _validate_management_active_store(store)
        if _migration_status(store) in _ACTIVE_MIGRATION_STATUSES:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        if not _active_store_matches(store, source):
            raise MemoryConflictError(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
        if store.index_status == LongTermMemoryIndexStatus.REINDEXING or store.old_collection_cleanup_status in _BLOCKING_CLEANUP_STATUSES:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)

        old_revision = await memory_embedding_revision_crud.get_by_job_id(
            db,
            uid=normalized_uid,
            job_id=normalized_migration_id,
        )
        if old_revision is None or old_revision.status not in {
            LongTermMemoryEmbeddingRevisionStatus.FAILED,
            LongTermMemoryEmbeddingRevisionStatus.CANCELLED,
        }:
            raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        next_revision = await memory_embedding_revision_crud.get_next_revision(db, uid=normalized_uid)
        target_collection = build_memory_collection_name(
            normalized_uid,
            target["signature"],
            next_revision,
            "target",
        )
        new_payload = {
            "from": dict(source),
            "target": {**target, "collection": target_collection, "revision": next_revision},
        }
        submission = await memory_job_manager.submit(
            db,
            uid=normalized_uid,
            operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            dedupe_key=_new_dedupe_key(old_job, prefix="embedding-migration-retry"),
            payload=new_payload,
            source_session_id=old_job.source_session_id,
            source_profile_id=old_job.source_profile_id,
            source_message_id=old_job.source_message_id,
            max_attempts=old_job.max_attempts,
            commit=False,
        )
        if not submission.created or submission.job.id is None:
            raise MemoryConflictError(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        started = await memory_store_crud.start_embedding_migration(
            db,
            uid=normalized_uid,
            job_id=submission.job.id,
            expected_active_revision=store.active_embedding_revision,
            target_embedding_channel_id=target["channel_id"],
            target_embedding_model_id=target["model_id"],
            target_embedding_dimensions=target["dimensions"],
            target_embedding_signature=target["signature"],
            target_collection_name=target_collection,
            migration_started_at=get_local_time(),
            commit=False,
        )
        if started is None:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        await memory_embedding_revision_crud.create(
            db,
            uid=normalized_uid,
            revision=next_revision,
            from_channel_id=source["channel_id"],
            from_model_id=source["model_id"],
            from_dimensions=source["dimensions"],
            from_signature=source["signature"],
            from_collection=source["collection"],
            to_channel_id=target["channel_id"],
            to_model_id=target["model_id"],
            to_dimensions=target["dimensions"],
            to_signature=target["signature"],
            to_collection=target_collection,
            confirmation_source_profile_id=old_revision.confirmation_source_profile_id,
            confirmation_source=old_revision.confirmation_source,
            embedding_selection_signature=old_revision.embedding_selection_signature,
            confirmed_at=get_local_time(),
            job_id=submission.job.id,
            status=LongTermMemoryEmbeddingRevisionStatus.CONFIRMED,
            commit=False,
        )
        if commit:
            await db.commit()
        return _submission_view(submission)
    except Exception:
        await db.rollback()
        raise


async def cancel_embedding_migration(db: AsyncSession, *, uid: str, migration_id: int) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    normalized_migration_id = _require_positive(migration_id, field="migration_id")
    try:
        job = await _get_job_or_raise(
            db,
            uid=normalized_uid,
            job_id=normalized_migration_id,
            error_key=ERR_MEMORY_MIGRATION_NOT_FOUND,
        )
        if job.operation != LongTermMemoryMutationOperation.EMBEDDING_MIGRATION:
            raise MemoryNotFoundError(ERR_MEMORY_MIGRATION_NOT_FOUND)
        return await _cancel_loaded_job(db, uid=normalized_uid, job=job)
    except Exception:
        await db.rollback()
        raise


def _job_operation(job: LongTermMemoryMutationJob) -> LongTermMemoryMutationOperation | None:
    try:
        return LongTermMemoryMutationOperation(job.operation)
    except (TypeError, ValueError):
        return None


def _blocking_state(
    reason: str | None,
    *,
    job: LongTermMemoryMutationJob | None = None,
    operation: LongTermMemoryMutationOperation | None = None,
) -> dict[str, Any]:
    resolved_operation = operation or (_job_operation(job) if job is not None else None)
    return {
        "blocked": reason is not None,
        "reason": reason,
        "job_id": job.id if job is not None else None,
        "operation": _json_value(resolved_operation),
    }


async def get_memory_settings(db: AsyncSession, *, uid: str) -> dict[str, Any]:
    normalized_uid = _normalize_uid(uid)
    store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
    active_count = await memory_record_crud.count_active(db, uid=normalized_uid)
    flat = _store_progress_view(store)
    target_revision = None
    migration_job = None
    if store is not None and store.migration_job_id is not None:
        migration_job = await memory_job_crud.get_by_id(db, uid=normalized_uid, job_id=store.migration_job_id)
        if migration_job is not None and isinstance(migration_job.payload, dict):
            target = migration_job.payload.get("target")
            if isinstance(target, dict):
                target_revision = target.get("revision")

    active = {
        "channel_id": flat.get("active_embedding_channel_id"),
        "model_id": flat.get("active_embedding_model_id"),
        "dimensions": flat.get("active_embedding_dimensions"),
        "signature": flat.get("active_embedding_signature"),
        "revision": flat.get("active_embedding_revision"),
        "collection": flat.get("active_collection_name"),
    }
    target = {
        "channel_id": flat.get("target_embedding_channel_id"),
        "model_id": flat.get("target_embedding_model_id"),
        "dimensions": flat.get("target_embedding_dimensions"),
        "signature": flat.get("target_embedding_signature"),
        "revision": target_revision,
        "collection": flat.get("target_collection_name"),
    }
    migration = {
        "job_id": flat.get("migration_job_id"),
        "status": flat.get("migration_status"),
        "snapshot_boundary": flat.get("migration_snapshot_boundary"),
        "cursor": flat.get("migration_cursor"),
        "total_count": flat.get("migration_total_count", 0),
        "success_count": flat.get("migration_success_count", 0),
        "failure_count": flat.get("migration_failure_count", 0),
        "error": flat.get("migration_error"),
        "started_at": flat.get("migration_started_at"),
        "finished_at": flat.get("migration_finished_at"),
    }
    delta = {
        "high_watermark": flat.get("migration_delta_high_watermark", 0),
        "applied_watermark": flat.get("migration_delta_applied_watermark", 0),
    }
    index = {
        "revision": flat.get("index_revision"),
        "status": flat.get("index_status"),
    }
    capacity = {
        "max_active_records": store.max_active_records if store is not None else MEMORY_MAX_ACTIVE_RECORDS,
        "organize_trigger_records": MEMORY_ORGANIZE_TRIGGER_RECORDS,
        "content_max_tokens": MEMORY_CONTENT_MAX_TOKENS,
        "active_record_count": active_count,
        "status": flat.get("capacity_status") or LongTermMemoryCapacityStatus.NORMAL.value,
    }
    organization = await get_organization_settings(
        db,
        uid=normalized_uid,
        snapshot_count=active_count,
    )
    unfinished_jobs = await memory_job_crud.list_unfinished_by_uid(db, uid=normalized_uid)
    current_job = unfinished_jobs[0] if unfinished_jobs else None
    active_organization_jobs = [job for job in unfinished_jobs if _job_operation(job) == LongTermMemoryMutationOperation.ORGANIZE]
    active_reindex_jobs = [job for job in unfinished_jobs if _job_operation(job) == LongTermMemoryMutationOperation.REINDEX]
    active_migration_jobs = [job for job in unfinished_jobs if _job_operation(job) == LongTermMemoryMutationOperation.EMBEDDING_MIGRATION]
    recent_organization_jobs = await memory_job_crud.get_page(
        db,
        uid=normalized_uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE,
        skip=0,
        limit=1,
    )
    current_organization_job = active_organization_jobs[0] if active_organization_jobs else None
    recent_organization_job = current_organization_job or (recent_organization_jobs[0] if recent_organization_jobs else None)
    organization.update(
        {
            "current_job_id": current_organization_job.id if current_organization_job is not None else None,
            "current_job": _job_view(current_organization_job),
            "recent_job_id": recent_organization_job.id if recent_organization_job is not None else None,
            "recent_job": _job_view(recent_organization_job),
        }
    )

    jobs_by_id = {job.id: job for job in unfinished_jobs if isinstance(job.id, int) and not isinstance(job.id, bool)}

    async def load_related_job(job_id: Any) -> LongTermMemoryMutationJob | None:
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
            return None
        if job_id in jobs_by_id:
            return jobs_by_id[job_id]
        return await memory_job_crud.get_by_id(db, uid=normalized_uid, job_id=job_id)

    migration_job_reference = active_migration_jobs[0] if active_migration_jobs else None
    if migration_job_reference is None and store is not None and _migration_status(store) in _ACTIVE_MIGRATION_STATUSES:
        migration_job_reference = await load_related_job(store.migration_job_id)
    cleanup_job_reference = None
    if store is not None and store.old_collection_cleanup_status in _BLOCKING_CLEANUP_STATUSES:
        cleanup_job_reference = await load_related_job(store.old_collection_cleanup_job_id)
    reindex_job_reference = active_reindex_jobs[0] if active_reindex_jobs else None

    active_store_ready = False
    if store is not None:
        try:
            _validate_management_active_store(store)
            active_store_ready = True
        except MemoryConflictError:
            active_store_ready = False

    maintenance_blocking = _blocking_state(None)
    if not active_store_ready:
        maintenance_blocking = _blocking_state("active_store_not_configured")
    elif reindex_job_reference is not None or (store is not None and store.index_status == LongTermMemoryIndexStatus.REINDEXING):
        maintenance_blocking = _blocking_state("reindex_active", job=reindex_job_reference, operation=LongTermMemoryMutationOperation.REINDEX)
    elif migration_job_reference is not None or (_migration_status(store) in _ACTIVE_MIGRATION_STATUSES if store is not None else False):
        maintenance_blocking = _blocking_state(
            "embedding_migration_active",
            job=migration_job_reference,
            operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        )
    elif cleanup_job_reference is not None or (store is not None and store.old_collection_cleanup_status in _BLOCKING_CLEANUP_STATUSES):
        maintenance_blocking = _blocking_state(
            "old_collection_cleanup_active",
            job=cleanup_job_reference,
        )

    organize_blocking = _blocking_state(None)
    if not active_store_ready:
        organize_blocking = _blocking_state("active_store_not_configured")
    elif current_organization_job is not None:
        organize_blocking = _blocking_state("organization_active", job=current_organization_job)
    elif maintenance_blocking["blocked"]:
        organize_blocking = maintenance_blocking
    elif organization.get("channel_id") is None or organization.get("model_id") is None:
        organize_blocking = _blocking_state("organization_model_not_configured")
    elif organization.get("validation_error") is not None:
        organize_blocking = _blocking_state("organization_model_invalid")

    cleanup = {
        "name": flat.get("old_collection_name"),
        "status": flat.get("old_collection_cleanup_status"),
        "job_id": flat.get("old_collection_cleanup_job_id"),
        "error": flat.get("old_collection_cleanup_error"),
        "at": flat.get("old_collection_cleanup_at"),
    }
    return {
        "configured": store is not None and active["revision"] not in (None, 0),
        "active": active,
        "target": target,
        "migration": migration,
        "delta": delta,
        "index": index,
        "capacity": capacity,
        "organization": organization,
        "blocking": {
            "organize": organize_blocking,
            "maintenance": maintenance_blocking,
        },
        "old_collection_cleanup": cleanup,
        "migration_job": _job_view(migration_job),
        "current_job": _job_view(current_job),
        "store": {
            **flat,
            "content_max_tokens": MEMORY_CONTENT_MAX_TOKENS,
            "active_record_count": active_count,
        },
    }


__all__ = [
    "cancel_embedding_migration",
    "cancel_job",
    "get_embedding_migration",
    "get_job",
    "get_memory",
    "get_memory_settings",
    "list_embedding_migrations",
    "list_jobs",
    "list_memory_history",
    "list_memories",
    "pin_memory",
    "submit_memory_organization",
    "update_memory_settings",
    "retry_embedding_migration",
    "retry_job",
    "unpin_memory",
]
