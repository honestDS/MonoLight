from __future__ import annotations

from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED,
    ERR_MEMORY_JOB_CANCELLATION_REQUESTED,
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_MIGRATION_SWITCH_FAILED,
    ERR_MEMORY_NOT_CONFIGURED,
)
from app.core.crud.memory import memory_store_crud
from app.core.crud.memory_job import memory_job_crud
from app.core.crud.memory_maintenance import (
    memory_maintenance_job_crud,
    memory_maintenance_record_crud,
    memory_maintenance_store_crud,
)
from app.core.i18n import t
from app.core.memory_jobs.executor import (
    MemoryJobCancelledError,
    MemoryJobExecutionContext,
    MemoryJobExecutionError,
    MemoryJobExecutionResult,
    MemoryJobLeaseLostError,
)
from app.core.memory_jobs.maintenance_lifecycle import (
    cleanup_old_collection,
    delete_cancelled_target_collection,
)
from app.core.memory_jobs.maintenance_state import (
    MAX_RECORD_ID,
    MIGRATION_ACTIVE_STATUSES,
    REINDEX_OPERATION,
    ValidationSnapshot,
    build_reindex_target_config,
    deterministic,
    list_current_records,
    matches_reindex_source,
    positive_int,
    read_recallable_records,
    read_snapshot_page,
    read_store,
    record_snapshot,
    require_job_id,
    retryable,
    validate_claim,
    validate_reindex_payload,
    validate_store_active,
)
from app.core.memory_jobs.maintenance_vector import (
    ensure_collection,
    reconcile_collection,
    upsert_records,
    validate_sample_query,
)
from app.models.memory import LongTermMemoryIndexStatus


async def _persist_reindex_payload(
    context: MemoryJobExecutionContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    async with context.session_factory() as db:
        updated = await memory_maintenance_job_crud.update_running_payload(
            db,
            uid=context.job.uid,
            job_id=require_job_id(context.job),
            payload=payload,
            owner=context.worker_id,
        )
        if updated is None:
            current = await memory_job_crud.get_active_claim(
                db,
                uid=context.job.uid,
                job_id=require_job_id(context.job),
                owner=context.worker_id,
            )
            if current is not None and current.cancel_requested_at is not None:
                raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
            raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
        return dict(updated.payload)


async def _prepare_reindex(
    context: MemoryJobExecutionContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    source = payload["from"]
    target = payload["target"]
    progress = payload["progress"]
    if progress["cursor"] != 0:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    store = await read_store(context)
    if store is None:
        raise deterministic(ERR_MEMORY_NOT_CONFIGURED)
    validate_store_active(store)
    if not matches_reindex_source(store, source) or store.index_status != LongTermMemoryIndexStatus.REINDEXING:
        raise retryable(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
    if store.migration_job_id is not None and store.migration_status in MIGRATION_ACTIVE_STATUSES:
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    if not progress["snapshot_initialized"]:
        async with context.session_factory() as db:
            boundary, total_count = await memory_maintenance_record_crud.get_recallable_snapshot(
                db,
                uid=context.job.uid,
            )
            await db.commit()
        payload["progress"] = {
            **progress,
            "snapshot_initialized": True,
            "snapshot_boundary": boundary,
            "total_count": total_count,
            "cursor": 0,
            "success_count": 0,
            "failure_count": 0,
        }
        payload = await _persist_reindex_payload(context, payload)
        progress = payload["progress"]
    config = build_reindex_target_config(source, target)
    await ensure_collection(uid=context.job.uid, config=config, purpose="reindex", reset=True)
    await context.checkpoint()
    payload["progress"] = {
        "phase": "building",
        "snapshot_initialized": True,
        "snapshot_boundary": progress["snapshot_boundary"],
        "cursor": progress["cursor"],
        "total_count": progress["total_count"],
        "success_count": progress["success_count"],
        "failure_count": progress["failure_count"],
    }
    return await _persist_reindex_payload(context, payload)


async def _build_reindex(
    context: MemoryJobExecutionContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    source = payload["from"]
    target = payload["target"]
    config = build_reindex_target_config(source, target)
    progress = payload["progress"]
    while True:
        await context.checkpoint()
        records = await read_snapshot_page(
            context,
            cursor=progress["cursor"],
            boundary=progress["snapshot_boundary"],
        )
        if not records:
            payload["progress"] = {**progress, "phase": "validating"}
            return await _persist_reindex_payload(context, payload)
        await upsert_records(context, config["collection"], records, config)
        await context.checkpoint()
        last_id = records[-1].id
        if not positive_int(last_id):
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        progress = {
            **progress,
            "cursor": last_id,
            "success_count": progress["success_count"] + len(records),
        }
        payload["progress"] = progress
        payload = await _persist_reindex_payload(context, payload)
        progress = payload["progress"]


async def _reindex_validation(
    context: MemoryJobExecutionContext,
    payload: dict[str, Any],
) -> ValidationSnapshot:
    source = payload["from"]
    target = payload["target"]
    records = await list_current_records(context)
    snapshot = await reconcile_collection(
        context,
        records=records,
        config=build_reindex_target_config(source, target),
        purpose="reindex",
    )
    await validate_sample_query(
        context,
        records=records,
        snapshot=snapshot,
        config=build_reindex_target_config(source, target),
    )
    return snapshot


async def _switch_reindex(
    context: MemoryJobExecutionContext,
    payload: dict[str, Any],
    validation: ValidationSnapshot,
) -> MemoryJobExecutionResult:
    source = payload["from"]
    target = payload["target"]
    job_id = require_job_id(context.job)
    async with context.session_factory() as db:
        claim = validate_claim(
            context,
            await memory_job_crud.get_active_claim(
                db,
                uid=context.job.uid,
                job_id=job_id,
                owner=context.worker_id,
            ),
            REINDEX_OPERATION,
        )
        store = await memory_store_crud.lock_for_mutation(db, uid=context.job.uid, commit=False)
        if store is None or not matches_reindex_source(store, source) or store.index_status != LongTermMemoryIndexStatus.REINDEXING:
            raise retryable(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
        if store.migration_job_id is not None and store.migration_status in MIGRATION_ACTIVE_STATUSES:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if claim.cancel_requested_at is not None:
            raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
        current_records = await read_recallable_records(db, uid=context.job.uid, boundary=MAX_RECORD_ID)
        current_snapshot_items = []
        for record in current_records:
            current_snapshot_items.append(record_snapshot(record, source["embedding_revision"]))
        current_snapshot = tuple(current_snapshot_items)
        if current_snapshot != validation.records:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        switching_payload = {**payload, "progress": {**payload["progress"], "phase": "switching"}}
        updated_job = await memory_maintenance_job_crud.update_running_payload(
            db,
            uid=context.job.uid,
            job_id=job_id,
            payload=switching_payload,
            owner=context.worker_id,
            commit=False,
        )
        if updated_job is None:
            raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
        switched = await memory_maintenance_store_crud.complete_reindex_switch(
            db,
            uid=context.job.uid,
            expected_active_revision=source["embedding_revision"],
            expected_active_collection_name=source["collection"],
            expected_index_revision=source["index_revision"],
            target_collection_name=target["collection"],
            target_index_revision=target["index_revision"],
            old_collection_cleanup_job_id=job_id,
            commit=False,
        )
        if switched is None:
            raise retryable(ERR_MEMORY_MIGRATION_SWITCH_FAILED)
        await db.commit()
    return await cleanup_old_collection(context, operation=REINDEX_OPERATION)


async def handle_reindex(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    try:
        payload = validate_reindex_payload(await context.checkpoint())
        store = await read_store(context)
        job_id = require_job_id(context.job)
        if store is not None:
            if store.old_collection_cleanup_job_id == job_id:
                if isinstance(store.old_collection_name, str) and store.old_collection_name:
                    return await cleanup_old_collection(context, operation=REINDEX_OPERATION)
        if payload["progress"]["phase"] == "preparing":
            payload = await _prepare_reindex(context, payload)
        if payload["progress"]["phase"] == "building":
            payload = await _build_reindex(context, payload)
        if payload["progress"]["phase"] == "validating":
            validation = await _reindex_validation(context, payload)
            return await _switch_reindex(context, payload, validation)
        if payload["progress"]["phase"] == "switching":
            return await cleanup_old_collection(context, operation=REINDEX_OPERATION)
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    except MemoryJobCancelledError:
        await delete_cancelled_target_collection(context, operation=REINDEX_OPERATION)
        raise
    except MemoryJobExecutionError as exc:
        if isinstance(exc, MemoryJobLeaseLostError):
            raise
        raise
    except Exception as exc:
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc


__all__ = ["handle_reindex"]
