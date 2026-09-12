from __future__ import annotations

from datetime import datetime

from app.core.constants import (
    ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED,
    ERR_MEMORY_JOB_CANCELLATION_REQUESTED,
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_PUBLICATION_FAILED,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import (
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.i18n import t
from app.core.memory import (
    append_memory_embedding_delta,
)
from app.core.memory.capacity import load_memory_capacity_snapshot
from app.core.memory_jobs.executor import (
    MemoryJobCancelledError,
    MemoryJobExecutionContext,
    MemoryJobExecutionResult,
    MemoryJobLeaseLostError,
)
from app.core.memory_jobs.manager import memory_job_manager
from app.core.memory_jobs.vector_cleanup import (
    create_superseded_vector_cleanup_job,
)
from app.models.memory import (
    LongTermMemoryCapacityStatus,
    LongTermMemoryEmbeddingDeltaAction,
    LongTermMemoryMutationOperation,
    LongTermMemoryRecordIndexStatus,
    LongTermMemorySource,
)

from .handler_contracts import (
    _deterministic,
    _MemoryOrganizationMergeSnapshot,
    _require_positive_int,
    _retryable,
    _validate_claim,
)
from .handler_organization_validation import (
    _validate_organization_merge_parent,
    _validate_organization_merge_payload,
)
from .handler_publication_validation import (
    _validate_organization_source_record,
    _validate_organization_store_snapshot,
)

__all__ = []


async def _publish_organization_merge(
    context: MemoryJobExecutionContext,
    snapshot: _MemoryOrganizationMergeSnapshot,
    vector_item_id: str,
) -> MemoryJobExecutionResult:
    await context.checkpoint()
    async with context.session_factory() as db:
        try:
            claim = _validate_claim(
                context,
                await memory_job_crud.get_active_claim(
                    db,
                    uid=snapshot.uid,
                    job_id=snapshot.job_id,
                    owner=snapshot.owner,
                ),
                LongTermMemoryMutationOperation.ORGANIZE_MERGE,
            )
            if claim.cancel_requested_at is not None:
                raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
            payload, source_payloads, target = _validate_organization_merge_payload(claim)
            if payload != snapshot.payload:
                raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
            await _validate_organization_merge_parent(db, job=claim, payload=payload)
            store = await memory_store_crud.lock_for_mutation(db, uid=snapshot.uid, commit=False)
            if store is None:
                raise _deterministic(ERR_MEMORY_NOT_CONFIGURED)
            (
                active_channel_id,
                active_model_id,
                active_dimensions,
                active_signature,
                active_revision,
                active_collection_name,
            ) = _validate_organization_store_snapshot(store, payload)
            if active_revision != snapshot.active_embedding_revision or store.index_revision != snapshot.index_revision:
                raise _deterministic(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
            if active_channel_id != snapshot.active_embedding_channel_id or active_model_id != snapshot.active_embedding_model_id or active_dimensions != snapshot.active_embedding_dimensions or active_signature != snapshot.active_embedding_signature or active_collection_name != snapshot.active_collection_name:
                raise _retryable(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
            records = await memory_record_crud.get_organization_group(
                db,
                uid=snapshot.uid,
                memory_ids=[source["memory_id"] for source in source_payloads],
            )
            if [record.id for record in records] != [source.memory_id for source in snapshot.sources]:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            current_sources = tuple(
                _validate_organization_source_record(
                    record,
                    uid=snapshot.uid,
                    job_id=snapshot.job_id,
                    source=source,
                    expected_snapshot=expected.record_snapshot,
                )
                for record, source, expected in zip(records, source_payloads, snapshot.sources, strict=True)
            )
            if current_sources != snapshot.sources:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)

            source_ids = {source.memory_id for source in snapshot.sources}
            key_record = await memory_record_crud.get_by_key(
                db,
                uid=snapshot.uid,
                memory_key=target["memory_key"],
            )
            hash_record = await memory_record_crud.get_by_content_hash(
                db,
                uid=snapshot.uid,
                content_hash=target["content_hash"],
            )
            if (key_record is not None and key_record.id not in source_ids) or (hash_record is not None and hash_record.id not in source_ids):
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            if await memory_job_manager.has_unfinished_target_identity(
                db,
                uid=snapshot.uid,
                memory_key=target["memory_key"],
                content_hash=target["content_hash"],
                exclude_job_id=snapshot.job_id,
            ):
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)

            source_states = tuple((source.memory_id, source.expected_version, source.pinned, source.vector_item_id) for source in snapshot.sources)
            if not await memory_record_crud.clear_organization_group_unique_fields(
                db,
                uid=snapshot.uid,
                source_states=source_states,
                job_id=snapshot.job_id,
                commit=False,
            ):
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)

            primary_version = snapshot.expected_version + 1
            publication = await memory_record_crud.publish_pending_version(
                db,
                uid=snapshot.uid,
                memory_id=snapshot.primary_memory_id,
                job_id=snapshot.job_id,
                expected_version=snapshot.expected_version,
                values={
                    "memory_key": target["memory_key"],
                    "memory_type": target["memory_type"],
                    "content": target["content"],
                    "content_token_count": target["content_token_count"],
                    "content_hash": target["content_hash"],
                    "source": LongTermMemorySource.AUTO_ORGANIZE,
                    "source_id": None,
                    "source_session_id": None,
                    "source_profile_id": None,
                    "source_message_id": None,
                    "source_job_id": snapshot.job_id,
                    "change_evidence": None,
                    "is_active": True,
                    "deleted_at": None,
                    "suppress_recall": False,
                    "suppressed_by_job_id": None,
                    "index_status": LongTermMemoryRecordIndexStatus.READY,
                    "vector_item_id": vector_item_id,
                },
                commit=False,
            )
            if publication is None:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            published_at = getattr(publication, "indexed_at", None)
            if not isinstance(published_at, datetime):
                raise _retryable(ERR_MEMORY_JOB_PUBLICATION_FAILED)
            await memory_revision_crud.create(
                db,
                uid=snapshot.uid,
                memory_id=snapshot.primary_memory_id,
                version=primary_version,
                memory_key=target["memory_key"],
                memory_type=target["memory_type"],
                content=target["content"],
                content_token_count=target["content_token_count"],
                content_hash=target["content_hash"],
                source=LongTermMemorySource.AUTO_ORGANIZE,
                source_id=None,
                source_session_id=None,
                source_profile_id=None,
                source_message_id=None,
                source_job_id=snapshot.job_id,
                change_evidence=None,
                published_at=published_at,
                commit=False,
            )

            cleanup_job_ids: list[int] = []
            tombstoned_memory_ids: list[int] = []
            for source in snapshot.sources:
                if source.memory_id == snapshot.primary_memory_id:
                    continue
                cleanup_job = await memory_job_manager.create_organization_cleanup_job(
                    db,
                    merge_job=claim,
                    memory_id=source.memory_id,
                    version=source.expected_version,
                    vector_item_id=source.vector_item_id,
                    record_snapshot=source.record_snapshot,
                    commit=False,
                )
                cleanup_job_id = _require_positive_int(cleanup_job.id, message_key=ERR_MEMORY_JOB_PUBLICATION_FAILED)
                if not await memory_record_crud.transfer_organization_source_to_cleanup(
                    db,
                    uid=snapshot.uid,
                    memory_id=source.memory_id,
                    version=source.expected_version,
                    pinned=source.pinned,
                    vector_item_id=source.vector_item_id,
                    merge_job_id=snapshot.job_id,
                    cleanup_job_id=cleanup_job_id,
                    commit=False,
                ):
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                cleanup_job_ids.append(cleanup_job_id)
                tombstoned_memory_ids.append(source.memory_id)

            await append_memory_embedding_delta(
                db,
                store=store,
                action=LongTermMemoryEmbeddingDeltaAction.UPSERT,
                memory_id=snapshot.primary_memory_id,
                memory_version=primary_version,
                source_mutation_job_id=snapshot.job_id,
                snapshot={
                    "version": primary_version,
                    "vector_item_id": vector_item_id,
                    "is_active": True,
                    "suppress_recall": False,
                    "index_status": LongTermMemoryRecordIndexStatus.READY.value,
                },
                commit=False,
            )
            for source in snapshot.sources:
                if source.memory_id == snapshot.primary_memory_id:
                    continue
                await append_memory_embedding_delta(
                    db,
                    store=store,
                    action=LongTermMemoryEmbeddingDeltaAction.DELETE,
                    memory_id=source.memory_id,
                    memory_version=source.expected_version,
                    source_mutation_job_id=snapshot.job_id,
                    snapshot={
                        "version": source.expected_version,
                        "vector_item_id": source.vector_item_id,
                        "is_active": False,
                    },
                    commit=False,
                )

            capacity = await load_memory_capacity_snapshot(
                db,
                snapshot.uid,
                store.max_active_records,
            )
            capacity_status = LongTermMemoryCapacityStatus.OVER_LIMIT if capacity.is_over_limit else LongTermMemoryCapacityStatus.NORMAL
            if (
                await memory_store_crud.update_by_uid(
                    db,
                    uid=snapshot.uid,
                    capacity_status=capacity_status,
                    commit=False,
                )
                is None
            ):
                raise _deterministic(ERR_MEMORY_JOB_PUBLICATION_FAILED)

            result = {
                "operation": LongTermMemoryMutationOperation.ORGANIZE_MERGE.value,
                "parent_job_id": snapshot.parent_job_id,
                "action": snapshot.action,
                "primary_memory_id": snapshot.primary_memory_id,
                "source_memory_ids": [source.memory_id for source in snapshot.sources],
                "version": primary_version,
                "vector_item_id": vector_item_id,
                "tombstoned_memory_ids": tombstoned_memory_ids,
                "cleanup_job_ids": cleanup_job_ids,
            }
            if snapshot.previous_vector_item_id and snapshot.previous_vector_item_id != vector_item_id:
                superseded_cleanup_job = await create_superseded_vector_cleanup_job(
                    db,
                    source_job=claim,
                    collection_name=snapshot.active_collection_name,
                    item_id=snapshot.previous_vector_item_id,
                )
                superseded_cleanup_job_id = _require_positive_int(
                    superseded_cleanup_job.id,
                    message_key=ERR_MEMORY_JOB_PUBLICATION_FAILED,
                )
                result["superseded_vector_cleanup_job_id"] = superseded_cleanup_job_id
            if not await memory_job_crud.mark_succeeded(
                db,
                uid=snapshot.uid,
                job_id=snapshot.job_id,
                owner=snapshot.owner,
                result=result,
                commit=False,
            ):
                current = await memory_job_crud.get_active_claim(
                    db,
                    uid=snapshot.uid,
                    job_id=snapshot.job_id,
                    owner=snapshot.owner,
                )
                if current is None:
                    raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
                if current.cancel_requested_at is not None:
                    raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
                raise _retryable(ERR_MEMORY_JOB_PUBLICATION_FAILED)
            await db.commit()
            return MemoryJobExecutionResult(result=result, finalized=True)
        except Exception:
            await db.rollback()
            raise
