from __future__ import annotations

from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED,
    ERR_MEMORY_JOB_CANCELLATION_REQUESTED,
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_MIGRATION_SWITCH_FAILED,
    ERR_MEMORY_NOT_CONFIGURED,
)
from app.core.crud.memory import (
    memory_embedding_delta_crud,
    memory_embedding_revision_crud,
    memory_store_crud,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.crud.memory_maintenance import (
    memory_maintenance_job_crud,
    memory_maintenance_record_crud,
    memory_maintenance_store_crud,
)
from app.core.crud.profile import profile_crud
from app.core.i18n import t
from app.core.memory_jobs.executor import (
    MemoryJobCancelledError,
    MemoryJobExecutionContext,
    MemoryJobExecutionError,
    MemoryJobExecutionResult,
    MemoryJobLeaseLostError,
    MemoryJobRetryableError,
)
from app.core.memory_jobs.maintenance_lifecycle import (
    cleanup_old_collection,
    delete_cancelled_target_collection,
    record_migration_retry_error,
)
from app.core.memory_jobs.maintenance_state import (
    BATCH_SIZE,
    MIGRATION_OPERATION,
    ValidationSnapshot,
    build_migration_target_config,
    deterministic,
    list_current_records,
    matches_migration_source,
    non_negative_int,
    positive_int,
    read_recallable_records,
    read_snapshot_page,
    read_store,
    record_snapshot,
    require_job_id,
    retryable,
    validate_claim,
    validate_migration_payload,
    validate_store_active,
)
from app.core.memory_jobs.maintenance_vector import (
    collection_items,
    ensure_collection,
    reconcile_collection,
    upsert_records,
    validate_sample_query,
)
from app.models.memory import (
    LongTermMemoryEmbeddingDeltaAction,
    LongTermMemoryEmbeddingDeltaStatus,
    LongTermMemoryEmbeddingRevisionStatus,
    LongTermMemoryMigrationStatus,
)
from app.providers.database.time import get_database_time
from app.providers.vector import async_delete_collection_items


async def _migration_revision_matches(
    context: MemoryJobExecutionContext,
    payload: dict[str, Any],
    job_id: int,
) -> Any:
    target = payload["target"]
    async with context.session_factory() as db:
        revision = await memory_embedding_revision_crud.get_by_revision(
            db,
            uid=context.job.uid,
            revision=target["revision"],
        )
        await db.commit()
    if revision is None or revision.job_id != job_id:
        raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    expected = {
        "from_channel_id": payload["from"]["channel_id"],
        "from_model_id": payload["from"]["model_id"],
        "from_dimensions": payload["from"]["dimensions"],
        "from_signature": payload["from"]["signature"],
        "from_collection": payload["from"]["collection"],
        "to_channel_id": target["channel_id"],
        "to_model_id": target["model_id"],
        "to_dimensions": target["dimensions"],
        "to_signature": target["signature"],
        "to_collection": target["collection"],
    }
    for field, value in expected.items():
        if getattr(revision, field, None) != value:
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    return revision


async def _prepare_migration(
    context: MemoryJobExecutionContext,
    payload: dict[str, Any],
) -> None:
    job_id = require_job_id(context.job)
    source = payload["from"]
    target = payload["target"]
    store = await read_store(context)
    if store is None:
        raise deterministic(ERR_MEMORY_NOT_CONFIGURED)
    validate_store_active(store)
    if store.migration_job_id != job_id or store.migration_status != LongTermMemoryMigrationStatus.PREPARING:
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    if not matches_migration_source(store, source):
        raise retryable(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
    if store.target_embedding_channel_id != target["channel_id"]:
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    if store.target_embedding_model_id != target["model_id"]:
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    if store.target_embedding_dimensions != target["dimensions"]:
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    if store.target_embedding_signature != target["signature"]:
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    if store.target_collection_name != target["collection"]:
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    revision = await _migration_revision_matches(context, payload, job_id)
    revision_status = LongTermMemoryEmbeddingRevisionStatus(revision.status)
    if revision_status == LongTermMemoryEmbeddingRevisionStatus.CONFIRMED:
        if revision.started_at is not None:
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        async with context.session_factory() as db:
            boundary, total_count = await memory_maintenance_record_crud.get_recallable_snapshot(
                db,
                uid=context.job.uid,
            )
            now = await get_database_time(db)
            updated = await memory_maintenance_store_crud.update_embedding_migration_progress(
                db,
                uid=context.job.uid,
                migration_job_id=job_id,
                migration_snapshot_boundary=boundary,
                migration_cursor=0,
                migration_total_count=total_count,
                migration_success_count=0,
                migration_failure_count=0,
                migration_status=LongTermMemoryMigrationStatus.PREPARING,
                migration_error=None,
                migration_started_at=now,
                migration_finished_at=None,
                commit=False,
            )
            if updated is None:
                raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            revision = await memory_embedding_revision_crud.update_by_revision(
                db,
                uid=context.job.uid,
                revision=target["revision"],
                status=LongTermMemoryEmbeddingRevisionStatus.RUNNING,
                started_at=now,
                error=None,
                commit=False,
            )
            if revision is None or revision.job_id != job_id:
                raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            await db.commit()
    elif revision_status == LongTermMemoryEmbeddingRevisionStatus.RUNNING:
        if revision.started_at is None or store.migration_started_at is None:
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if (
            store.migration_snapshot_boundary is None
            or not non_negative_int(store.migration_snapshot_boundary)
            or not non_negative_int(store.migration_total_count)
            or not non_negative_int(store.migration_cursor)
            or not non_negative_int(store.migration_success_count)
            or not non_negative_int(store.migration_failure_count)
            or store.migration_cursor != 0
            or store.migration_success_count != 0
            or store.migration_failure_count != 0
        ):
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    else:
        raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    config = build_migration_target_config(target, store.index_revision + 1)
    await ensure_collection(uid=context.job.uid, config=config, purpose="migration", reset=True)
    await context.checkpoint()
    async with context.session_factory() as db:
        updated = await memory_maintenance_store_crud.update_embedding_migration_progress(
            db,
            uid=context.job.uid,
            migration_job_id=job_id,
            migration_status=LongTermMemoryMigrationStatus.BUILDING,
            commit=False,
        )
        if updated is None:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        await db.commit()


async def _build_migration(
    context: MemoryJobExecutionContext,
    payload: dict[str, Any],
) -> None:
    job_id = require_job_id(context.job)
    target = payload["target"]
    while True:
        await context.checkpoint()
        store = await read_store(context)
        if store is None or store.migration_job_id != job_id:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if store.migration_status != LongTermMemoryMigrationStatus.BUILDING:
            return
        cursor = store.migration_cursor
        if not non_negative_int(cursor):
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        boundary = store.migration_snapshot_boundary
        if not non_negative_int(boundary):
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if cursor > boundary:
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        records = await read_snapshot_page(
            context,
            cursor=cursor,
            boundary=boundary,
        )
        if not records:
            async with context.session_factory() as db:
                updated = await memory_maintenance_store_crud.update_embedding_migration_progress(
                    db,
                    uid=context.job.uid,
                    migration_job_id=job_id,
                    migration_status=LongTermMemoryMigrationStatus.CATCHING_UP,
                    commit=False,
                )
                if updated is None:
                    raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                await db.commit()
            return
        config = build_migration_target_config(target, store.index_revision + 1)
        await upsert_records(context, config["collection"], records, config)
        await context.checkpoint()
        last_id = records[-1].id
        if not positive_int(last_id):
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        async with context.session_factory() as db:
            updated = await memory_maintenance_store_crud.update_embedding_migration_progress(
                db,
                uid=context.job.uid,
                migration_job_id=job_id,
                migration_cursor=last_id,
                migration_success_count=store.migration_success_count + len(records),
                commit=False,
            )
            if updated is None:
                raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            await db.commit()


async def _apply_migration_delta(
    context: MemoryJobExecutionContext,
    delta: Any,
    config: dict[str, Any],
) -> None:
    if not positive_int(delta.memory_id):
        raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    try:
        action = LongTermMemoryEmbeddingDeltaAction(delta.action)
    except (TypeError, ValueError) as exc:
        raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    item_ids: list[str] = []
    items = await collection_items(config["collection"])
    for item_id, item in items.items():
        metadata = item[1]
        if metadata.get("memory_id") == delta.memory_id:
            item_ids.append(item_id)
    if action in {
        LongTermMemoryEmbeddingDeltaAction.DELETE,
        LongTermMemoryEmbeddingDeltaAction.SUPPRESS,
    }:
        if item_ids:
            await async_delete_collection_items(config["collection"], item_ids, batch_size=BATCH_SIZE)
        return
    if action != LongTermMemoryEmbeddingDeltaAction.UPSERT:
        raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    async with context.session_factory() as db:
        record = await memory_maintenance_record_crud.get_recallable_by_memory_id(
            db,
            uid=context.job.uid,
            memory_id=delta.memory_id,
        )
        await db.commit()
    if record is not None:
        await upsert_records(context, config["collection"], [record], config)
        current_id = record.vector_item_id
        stale_ids: list[str] = []
        for item_id in item_ids:
            if item_id != current_id:
                stale_ids.append(item_id)
        if stale_ids:
            await async_delete_collection_items(config["collection"], stale_ids, batch_size=BATCH_SIZE)
    elif item_ids:
        await async_delete_collection_items(config["collection"], item_ids, batch_size=BATCH_SIZE)


def _validate_delta_batch(deltas: list[Any], applied: int, high: int) -> None:
    expected_count = min(BATCH_SIZE, high - applied)
    if len(deltas) != expected_count:
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    for offset, delta in enumerate(deltas, start=1):
        if delta.sequence != applied + offset:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if delta.status not in {
            LongTermMemoryEmbeddingDeltaStatus.PENDING,
            LongTermMemoryEmbeddingDeltaStatus.APPLIED,
        }:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)


async def _catch_up_migration(
    context: MemoryJobExecutionContext,
    payload: dict[str, Any],
) -> None:
    job_id = require_job_id(context.job)
    stable_observations = 0
    observed_high: int | None = None
    while stable_observations < 2:
        await context.checkpoint()
        store = await read_store(context)
        if store is None:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if store.migration_job_id != job_id:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if store.migration_status != LongTermMemoryMigrationStatus.CATCHING_UP:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        applied = store.migration_delta_applied_watermark
        high = store.migration_delta_high_watermark
        if not non_negative_int(applied) or not non_negative_int(high) or applied > high:
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if applied < high:
            async with context.session_factory() as db:
                deltas = await memory_embedding_delta_crud.list_by_migration_job(
                    db,
                    uid=context.job.uid,
                    migration_job_id=job_id,
                    sequence_start=applied + 1,
                    sequence_end=high,
                    limit=BATCH_SIZE,
                )
                await db.commit()
            _validate_delta_batch(deltas, applied, high)
            config = build_migration_target_config(payload["target"], store.index_revision + 1)
            for delta in deltas:
                await context.checkpoint()
                if delta.status == LongTermMemoryEmbeddingDeltaStatus.PENDING:
                    await _apply_migration_delta(context, delta, config)
                await context.checkpoint()
                async with context.session_factory() as db:
                    updated_delta = await memory_embedding_delta_crud.update_by_sequence(
                        db,
                        uid=context.job.uid,
                        migration_job_id=job_id,
                        sequence=delta.sequence,
                        status=LongTermMemoryEmbeddingDeltaStatus.APPLIED,
                        applied_at=await get_database_time(db),
                        commit=False,
                    )
                    if updated_delta is None:
                        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                    advanced = await memory_maintenance_store_crud.advance_migration_delta_applied_watermark(
                        db,
                        uid=context.job.uid,
                        migration_job_id=job_id,
                        expected_watermark=delta.sequence - 1,
                        new_watermark=delta.sequence,
                        commit=False,
                    )
                    if advanced is None:
                        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                    await db.commit()
            stable_observations = 0
            observed_high = None
            continue
        if observed_high == high and applied == high:
            stable_observations += 1
        else:
            observed_high = high
            stable_observations = 1
    async with context.session_factory() as db:
        updated = await memory_maintenance_store_crud.update_embedding_migration_progress(
            db,
            uid=context.job.uid,
            migration_job_id=job_id,
            migration_status=LongTermMemoryMigrationStatus.VALIDATING,
            commit=False,
        )
        if updated is None:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        await db.commit()


async def _migration_validation(
    context: MemoryJobExecutionContext,
    payload: dict[str, Any],
) -> ValidationSnapshot:
    target = payload["target"]
    store = await read_store(context)
    if store is None:
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    records = await list_current_records(context)
    config = build_migration_target_config(target, store.index_revision + 1)
    snapshot = await reconcile_collection(
        context,
        records=records,
        config=config,
        purpose="migration",
    )
    await validate_sample_query(context, records=records, snapshot=snapshot, config=config)
    return snapshot


async def _switch_migration(
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
            MIGRATION_OPERATION,
        )
        store = await memory_store_crud.lock_for_mutation(db, uid=context.job.uid, commit=False)
        if store is None or store.migration_job_id != job_id or not matches_migration_source(store, source):
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if store.migration_status != LongTermMemoryMigrationStatus.VALIDATING:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if store.migration_delta_high_watermark != store.migration_delta_applied_watermark:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if claim.cancel_requested_at is not None:
            raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
        current_records = await read_recallable_records(db, uid=context.job.uid)
        current_snapshot_items = []
        for record in current_records:
            current_snapshot_items.append(record_snapshot(record, target["revision"]))
        current_snapshot = tuple(current_snapshot_items)
        if current_snapshot != validation.records:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        updated_job = await memory_maintenance_job_crud.update_running_payload(
            db,
            uid=context.job.uid,
            job_id=job_id,
            payload=payload,
            owner=context.worker_id,
            commit=False,
        )
        if updated_job is None:
            current_claim = await memory_job_crud.get_active_claim(
                db,
                uid=context.job.uid,
                job_id=job_id,
                owner=context.worker_id,
            )
            if current_claim is not None and current_claim.cancel_requested_at is not None:
                raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
            raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
        revision = await memory_embedding_revision_crud.get_by_revision(
            db,
            uid=context.job.uid,
            revision=target["revision"],
        )
        if revision is None:
            raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        now = await get_database_time(db)
        switching = await memory_maintenance_store_crud.update_embedding_migration_progress(
            db,
            uid=context.job.uid,
            migration_job_id=job_id,
            migration_status=LongTermMemoryMigrationStatus.SWITCHING,
            commit=False,
        )
        if switching is None:
            raise retryable(ERR_MEMORY_MIGRATION_SWITCH_FAILED)
        revision_result = await memory_embedding_revision_crud.update_by_revision(
            db,
            uid=context.job.uid,
            revision=target["revision"],
            status=LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED,
            result={
                "collection": target["collection"],
                "revision": target["revision"],
                "count": validation.count,
                "success_count": validation.success_count,
            },
            finished_at=now,
            error=None,
            commit=False,
        )
        if revision_result is None:
            raise retryable(ERR_MEMORY_MIGRATION_SWITCH_FAILED)
        switched = await memory_maintenance_store_crud.complete_embedding_migration_switch(
            db,
            uid=context.job.uid,
            migration_job_id=job_id,
            expected_active_channel_id=source["channel_id"],
            expected_active_model_id=source["model_id"],
            expected_active_dimensions=source["dimensions"],
            expected_active_signature=source["signature"],
            expected_active_revision=source["revision"],
            expected_active_collection_name=source["collection"],
            expected_index_revision=store.index_revision,
            target_channel_id=target["channel_id"],
            target_model_id=target["model_id"],
            target_dimensions=target["dimensions"],
            target_signature=target["signature"],
            target_revision=target["revision"],
            target_index_revision=store.index_revision + 1,
            target_collection_name=target["collection"],
            old_collection_cleanup_job_id=job_id,
            finished_at=now,
            owner=context.worker_id,
            commit=False,
        )
        if switched is None:
            current_claim = await memory_job_crud.get_active_claim(
                db,
                uid=context.job.uid,
                job_id=job_id,
                owner=context.worker_id,
            )
            if current_claim is None:
                raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
            if current_claim.cancel_requested_at is not None:
                raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
            raise retryable(ERR_MEMORY_MIGRATION_SWITCH_FAILED)
        await profile_crud.normalize_memory_selection_by_uid(
            db,
            uid=context.job.uid,
            embedding_channel_id=target["channel_id"],
            embedding_model_id=target["model_id"],
            commit=False,
        )
        await db.commit()
    return await cleanup_old_collection(context, operation=MIGRATION_OPERATION)


async def handle_embedding_migration(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    try:
        payload = validate_migration_payload(await context.checkpoint())
        store = await read_store(context)
        job_id = require_job_id(context.job)
        if store is not None:
            if store.old_collection_cleanup_job_id == job_id:
                if isinstance(store.old_collection_name, str) and store.old_collection_name:
                    return await cleanup_old_collection(context, operation=MIGRATION_OPERATION)
        if store is None or store.migration_job_id != job_id:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        status = LongTermMemoryMigrationStatus(store.migration_status)
        if status == LongTermMemoryMigrationStatus.PREPARING:
            await _prepare_migration(context, payload)
            store = await read_store(context)
            status = LongTermMemoryMigrationStatus(store.migration_status)
        if status == LongTermMemoryMigrationStatus.BUILDING:
            await _build_migration(context, payload)
            store = await read_store(context)
            status = LongTermMemoryMigrationStatus(store.migration_status)
        if status == LongTermMemoryMigrationStatus.CATCHING_UP:
            await _catch_up_migration(context, payload)
            store = await read_store(context)
            status = LongTermMemoryMigrationStatus(store.migration_status)
        if status == LongTermMemoryMigrationStatus.VALIDATING:
            validation = await _migration_validation(context, payload)
            return await _switch_migration(context, payload, validation)
        if status == LongTermMemoryMigrationStatus.SWITCHING:
            raise retryable(ERR_MEMORY_MIGRATION_SWITCH_FAILED)
        raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    except MemoryJobCancelledError:
        await delete_cancelled_target_collection(context, operation=MIGRATION_OPERATION)
        raise
    except MemoryJobExecutionError as exc:
        if isinstance(exc, MemoryJobLeaseLostError):
            raise
        if isinstance(exc, MemoryJobRetryableError) and context.job.attempt_count < context.job.max_attempts:
            await record_migration_retry_error(context, ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        raise
    except Exception as exc:
        if context.job.attempt_count < context.job.max_attempts:
            await record_migration_retry_error(context, ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc


__all__ = ["handle_embedding_migration"]
