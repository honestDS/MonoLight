from __future__ import annotations

from datetime import datetime

from app.core.constants import (
    ERR_MEMORY_JOB_CANCELLATION_REQUESTED,
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_PUBLICATION_FAILED,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_VERSION_CONFLICT,
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
    build_memory_active_mutation_key,
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
    LongTermMemoryEmbeddingDeltaAction,
    LongTermMemoryMutationOperation,
    LongTermMemoryRecordIndexStatus,
)

from .handler_contracts import (
    _deterministic,
    _MemoryPublicationSnapshot,
    _MemoryReplacementSnapshot,
    _normalize_publication_for_job,
    _require_non_negative_int,
    _require_positive_int,
    _retryable,
    _validate_claim,
)
from .handler_publication_validation import (
    _validate_active_key,
    _validate_publication_capacity,
    _validate_record_state,
    _validate_replacement_placeholder,
    _validate_unique_publication,
)
from .handler_replacement_validation import (
    _store_matches_snapshot,
    _validate_active_store,
    _validate_replacement_candidate_record,
    _validate_replacement_capacity,
    _validate_replacement_payload,
    _validate_replacement_store_snapshot,
)

__all__ = []


async def _publish_version(
    context: MemoryJobExecutionContext,
    snapshot: _MemoryPublicationSnapshot,
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
                snapshot.operation,
            )
            if claim.cancel_requested_at is not None:
                raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
            payload = _normalize_publication_for_job(claim, snapshot.operation)
            if payload != snapshot.payload:
                raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
            memory_id = _require_positive_int(claim.memory_id)
            if snapshot.operation == LongTermMemoryMutationOperation.CREATE:
                if claim.expected_version is not None:
                    raise _deterministic(ERR_MEMORY_VERSION_CONFLICT)
                claim_expected_version = 0
            else:
                claim_expected_version = _require_non_negative_int(claim.expected_version)
            if memory_id != snapshot.memory_id or claim_expected_version != snapshot.expected_version:
                raise _deterministic(ERR_MEMORY_VERSION_CONFLICT)
            _validate_active_key(claim, snapshot.operation, snapshot.uid, payload, memory_id)

            store = await memory_store_crud.lock_for_mutation(db, uid=snapshot.uid, commit=False)
            if store is None:
                raise _deterministic(ERR_MEMORY_NOT_CONFIGURED)
            _, _, _, _, _, _, max_active_records = _validate_active_store(store)
            capacity = await load_memory_capacity_snapshot(db, snapshot.uid, max_active_records)
            _store_matches_snapshot(store, snapshot)
            record = await memory_record_crud.get_by_id(db, uid=snapshot.uid, memory_id=memory_id)
            _validate_record_state(
                record,
                operation=snapshot.operation,
                job_id=snapshot.job_id,
                expected_version=snapshot.expected_version,
                payload=payload,
            )
            _validate_publication_capacity(
                capacity,
                operation=snapshot.operation,
                record=record,
                payload=payload,
                max_active_records=max_active_records,
            )
            await _validate_unique_publication(db, uid=snapshot.uid, memory_id=memory_id, payload=payload)

            next_version = snapshot.expected_version + 1
            published = await memory_record_crud.publish_pending_version(
                db,
                uid=snapshot.uid,
                memory_id=memory_id,
                job_id=snapshot.job_id,
                expected_version=snapshot.expected_version,
                values={
                    "memory_key": payload["memory_key"],
                    "memory_type": payload["memory_type"],
                    "content": payload["content"],
                    "content_token_count": payload["content_token_count"],
                    "content_hash": payload["content_hash"],
                    "source": payload["source"],
                    "source_id": payload["source_id"],
                    "source_session_id": payload["source_session_id"],
                    "source_profile_id": payload["source_profile_id"],
                    "source_message_id": payload["source_message_id"],
                    "source_job_id": snapshot.job_id,
                    "change_evidence": payload["change_evidence"],
                    "is_active": True,
                    "deleted_at": None,
                    "suppress_recall": False,
                    "suppressed_by_job_id": None,
                    "index_status": LongTermMemoryRecordIndexStatus.READY,
                    "vector_item_id": vector_item_id,
                },
                commit=False,
            )
            if published is None:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            published_at = getattr(published, "indexed_at", None)
            if not isinstance(published_at, datetime):
                raise _retryable(ERR_MEMORY_JOB_PUBLICATION_FAILED)
            await memory_revision_crud.create(
                db,
                uid=snapshot.uid,
                memory_id=memory_id,
                version=next_version,
                memory_key=payload["memory_key"],
                memory_type=payload["memory_type"],
                content=payload["content"],
                content_token_count=payload["content_token_count"],
                content_hash=payload["content_hash"],
                source=payload["source"],
                source_id=payload["source_id"],
                source_session_id=payload["source_session_id"],
                source_profile_id=payload["source_profile_id"],
                source_message_id=payload["source_message_id"],
                source_job_id=snapshot.job_id,
                change_evidence=payload["change_evidence"],
                published_at=published_at,
                commit=False,
            )
            await append_memory_embedding_delta(
                db,
                store=store,
                action=LongTermMemoryEmbeddingDeltaAction.UPSERT,
                memory_id=memory_id,
                memory_version=next_version,
                source_mutation_job_id=snapshot.job_id,
                snapshot={
                    "version": next_version,
                    "vector_item_id": vector_item_id,
                    "is_active": True,
                    "suppress_recall": False,
                    "index_status": LongTermMemoryRecordIndexStatus.READY.value,
                },
                commit=False,
            )
            result = {
                "memory_id": memory_id,
                "version": next_version,
                "vector_item_id": vector_item_id,
                "operation": snapshot.operation.value,
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


async def _publish_replacement(
    context: MemoryJobExecutionContext,
    snapshot: _MemoryReplacementSnapshot,
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
                LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            )
            if claim.cancel_requested_at is not None:
                raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))
            publication, candidate, store_snapshot = _validate_replacement_payload(claim)
            memory_id = _require_positive_int(claim.memory_id, message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID)
            if claim.expected_version is not None or memory_id != snapshot.memory_id or publication != snapshot.publication or candidate != snapshot.candidate or store_snapshot != snapshot.store:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            active_key = build_memory_active_mutation_key(snapshot.uid, memory_key=publication["memory_key"])
            if claim.active_mutation_key != active_key:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)

            store = await memory_store_crud.lock_for_mutation(db, uid=snapshot.uid, commit=False)
            if store is None:
                raise _deterministic(ERR_MEMORY_NOT_CONFIGURED)
            _validate_replacement_store_snapshot(store, store_snapshot)
            await _validate_replacement_capacity(db, uid=snapshot.uid, store=store_snapshot)
            candidate_record = await memory_record_crud.get_by_id(
                db,
                uid=snapshot.uid,
                memory_id=candidate.memory_id,
            )
            _validate_replacement_candidate_record(
                candidate_record,
                uid=snapshot.uid,
                job_id=snapshot.job_id,
                candidate=candidate,
            )
            replacement_record = await memory_record_crud.get_by_id(db, uid=snapshot.uid, memory_id=memory_id)
            _validate_replacement_placeholder(
                replacement_record,
                uid=snapshot.uid,
                job_id=snapshot.job_id,
                memory_id=memory_id,
            )
            await _validate_unique_publication(db, uid=snapshot.uid, memory_id=memory_id, payload=publication)
            if claim.cancel_requested_at is not None:
                raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))

            published = await memory_record_crud.publish_pending_version(
                db,
                uid=snapshot.uid,
                memory_id=memory_id,
                job_id=snapshot.job_id,
                expected_version=0,
                values={
                    "memory_key": publication["memory_key"],
                    "memory_type": publication["memory_type"],
                    "content": publication["content"],
                    "content_token_count": publication["content_token_count"],
                    "content_hash": publication["content_hash"],
                    "vector_item_id": vector_item_id,
                    "source": publication["source"],
                    "source_id": publication["source_id"],
                    "source_session_id": publication["source_session_id"],
                    "source_profile_id": publication["source_profile_id"],
                    "source_message_id": publication["source_message_id"],
                    "source_job_id": snapshot.job_id,
                    "change_evidence": publication["change_evidence"],
                    "is_active": True,
                    "suppress_recall": False,
                    "suppressed_by_job_id": None,
                    "index_status": LongTermMemoryRecordIndexStatus.READY,
                    "deleted_at": None,
                },
                commit=False,
            )
            if published is None or published.id != memory_id:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            published_at = getattr(published, "indexed_at", None)
            if not isinstance(published_at, datetime):
                raise _retryable(ERR_MEMORY_JOB_PUBLICATION_FAILED)
            await memory_revision_crud.create(
                db,
                uid=snapshot.uid,
                memory_id=memory_id,
                version=1,
                memory_key=publication["memory_key"],
                memory_type=publication["memory_type"],
                content=publication["content"],
                content_token_count=publication["content_token_count"],
                content_hash=publication["content_hash"],
                source=publication["source"],
                source_id=publication["source_id"],
                source_session_id=publication["source_session_id"],
                source_profile_id=publication["source_profile_id"],
                source_message_id=publication["source_message_id"],
                source_job_id=snapshot.job_id,
                change_evidence=publication["change_evidence"],
                published_at=published_at,
                commit=False,
            )

            cleanup_job = await memory_job_manager.create_eviction_cleanup_job(
                db,
                replacement_job=claim,
                commit=False,
            )
            cleanup_job_id = _require_positive_int(cleanup_job.id, message_key=ERR_MEMORY_JOB_PUBLICATION_FAILED)
            await append_memory_embedding_delta(
                db,
                store=store,
                action=LongTermMemoryEmbeddingDeltaAction.UPSERT,
                memory_id=memory_id,
                memory_version=1,
                source_mutation_job_id=snapshot.job_id,
                snapshot={
                    "version": 1,
                    "vector_item_id": vector_item_id,
                    "is_active": True,
                    "suppress_recall": False,
                    "index_status": LongTermMemoryRecordIndexStatus.READY.value,
                },
                commit=False,
            )
            await append_memory_embedding_delta(
                db,
                store=store,
                action=LongTermMemoryEmbeddingDeltaAction.DELETE,
                memory_id=candidate.memory_id,
                memory_version=candidate.version,
                source_mutation_job_id=snapshot.job_id,
                snapshot={
                    "version": candidate.version,
                    "vector_item_id": candidate.vector_item_id,
                    "is_active": False,
                },
                commit=False,
            )
            result = {
                "operation": LongTermMemoryMutationOperation.CREATE_WITH_EVICTION.value,
                "memory_id": memory_id,
                "version": 1,
                "vector_item_id": vector_item_id,
                "evicted_memory_id": candidate.memory_id,
                "cleanup_job_id": cleanup_job_id,
            }
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
