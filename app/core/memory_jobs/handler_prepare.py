from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import (
    memory_record_crud,
    memory_store_crud,
)
from app.core.embedding.common import (
    load_embedding_runtime_config,
)
from app.core.i18n import t
from app.core.memory import (
    build_memory_active_mutation_key,
    build_memory_record_snapshot,
)
from app.core.memory.capacity import load_memory_capacity_snapshot
from app.core.memory_jobs.executor import (
    MemoryJobExecutionContext,
    MemoryJobLeaseLostError,
)
from app.models.memory import (
    LongTermMemoryMutationOperation,
    LongTermMemorySource,
)

from .handler_contracts import (
    _deterministic,
    _MemoryDeleteCleanupSnapshot,
    _MemoryOrganizationMergeSnapshot,
    _MemoryPublicationSnapshot,
    _MemoryReplacementSnapshot,
    _normalize_publication_for_job,
    _require_job_id,
    _require_non_negative_int,
    _require_positive_int,
    _validate_claim,
)
from .handler_organization_validation import (
    _validate_delete_payload,
    _validate_organization_cleanup_source,
    _validate_organization_merge_parent,
    _validate_organization_merge_payload,
)
from .handler_publication_validation import (
    _load_organization_runtime_config,
    _validate_active_key,
    _validate_organization_source_record,
    _validate_organization_store_snapshot,
    _validate_publication_capacity,
    _validate_record_state,
    _validate_replacement_placeholder,
    _validate_unique_publication,
)
from .handler_replacement_validation import (
    _enum_value,
    _validate_active_store,
    _validate_replacement_candidate_record,
    _validate_replacement_capacity,
    _validate_replacement_payload,
    _validate_replacement_store_snapshot,
)

__all__ = []


async def _prepare_publication(
    context: MemoryJobExecutionContext,
    operation: LongTermMemoryMutationOperation,
) -> _MemoryPublicationSnapshot:
    checkpoint_job = await context.checkpoint()
    job_id = _require_job_id(checkpoint_job)
    async with context.session_factory() as db:
        try:
            claim = _validate_claim(
                context,
                await memory_job_crud.get_active_claim(
                    db,
                    uid=context.job.uid,
                    job_id=job_id,
                    owner=context.worker_id,
                ),
                operation,
            )
            payload = _normalize_publication_for_job(claim, operation)
            uid = claim.uid
            memory_id = claim.memory_id
            if operation == LongTermMemoryMutationOperation.CREATE:
                if claim.expected_version is not None:
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                _validate_active_key(claim, operation, uid, payload, memory_id)
            else:
                memory_id = _require_positive_int(claim.memory_id)
                expected_version = _require_non_negative_int(claim.expected_version)
                _validate_active_key(claim, operation, uid, payload, memory_id)

            store = await memory_store_crud.lock_for_mutation(db, uid=uid, commit=False)
            if store is None:
                raise _deterministic(ERR_MEMORY_NOT_CONFIGURED)
            (
                active_channel_id,
                active_model_id,
                active_dimensions,
                active_signature,
                active_revision,
                active_collection_name,
                max_active_records,
            ) = _validate_active_store(store)
            capacity = await load_memory_capacity_snapshot(db, uid, max_active_records)

            if operation == LongTermMemoryMutationOperation.CREATE:
                _validate_publication_capacity(
                    capacity,
                    operation=operation,
                    record=None,
                    payload=payload,
                    max_active_records=max_active_records,
                )
                if memory_id is None:
                    placeholder = await memory_record_crud.create_pending_placeholder(
                        db,
                        uid=uid,
                        job_id=job_id,
                        commit=False,
                    )
                    if placeholder.id is None:
                        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                    if not await memory_job_crud.assign_create_memory_id(
                        db,
                        uid=uid,
                        job_id=job_id,
                        memory_id=placeholder.id,
                        owner=context.worker_id,
                        commit=False,
                    ):
                        current = await memory_job_crud.get_active_claim(
                            db,
                            uid=uid,
                            job_id=job_id,
                            owner=context.worker_id,
                        )
                        if current is None:
                            raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
                        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                    memory_id = placeholder.id
                    expected_version = 0
                else:
                    expected_version = 0
            else:
                expected_version = _require_non_negative_int(claim.expected_version)

            record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)
            _validate_record_state(
                record,
                operation=operation,
                job_id=job_id,
                expected_version=expected_version,
                payload=payload,
            )
            if operation != LongTermMemoryMutationOperation.CREATE:
                _validate_publication_capacity(
                    capacity,
                    operation=operation,
                    record=record,
                    payload=payload,
                    max_active_records=max_active_records,
                )
            await _validate_unique_publication(db, uid=uid, memory_id=memory_id, payload=payload)

            runtime_config = await load_embedding_runtime_config(db, active_channel_id, active_model_id)
            updated_at_text = datetime.now(UTC).isoformat()
            await db.commit()
            return _MemoryPublicationSnapshot(
                uid=uid,
                job_id=job_id,
                owner=context.worker_id,
                operation=operation,
                memory_id=memory_id,
                expected_version=expected_version,
                payload=payload,
                runtime_config=runtime_config,
                active_embedding_channel_id=active_channel_id,
                active_embedding_model_id=active_model_id,
                active_embedding_dimensions=active_dimensions,
                active_embedding_signature=active_signature,
                active_embedding_revision=active_revision,
                active_collection_name=active_collection_name,
                previous_vector_item_id=getattr(record, "vector_item_id", None),
                updated_at=updated_at_text,
            )
        except Exception:
            await db.rollback()
            raise


async def _prepare_replacement(context: MemoryJobExecutionContext) -> _MemoryReplacementSnapshot:
    checkpoint_job = await context.checkpoint()
    job_id = _require_job_id(checkpoint_job)
    async with context.session_factory() as db:
        try:
            claim = _validate_claim(
                context,
                await memory_job_crud.get_active_claim(
                    db,
                    uid=context.job.uid,
                    job_id=job_id,
                    owner=context.worker_id,
                ),
                LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            )
            publication, candidate, store_snapshot = _validate_replacement_payload(claim)
            if claim.expected_version is not None:
                raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
            active_key = build_memory_active_mutation_key(claim.uid, memory_key=publication["memory_key"])
            if claim.active_mutation_key != active_key:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)

            store = await memory_store_crud.lock_for_mutation(db, uid=claim.uid, commit=False)
            if store is None:
                raise _deterministic(ERR_MEMORY_NOT_CONFIGURED)
            _validate_replacement_store_snapshot(store, store_snapshot)
            await _validate_replacement_capacity(db, uid=claim.uid, store=store_snapshot)

            candidate_record = await memory_record_crud.get_by_id(
                db,
                uid=claim.uid,
                memory_id=candidate.memory_id,
            )
            _validate_replacement_candidate_record(
                candidate_record,
                uid=claim.uid,
                job_id=job_id,
                candidate=candidate,
            )
            if claim.memory_id is None:
                placeholder = await memory_record_crud.create_pending_placeholder(
                    db,
                    uid=claim.uid,
                    job_id=job_id,
                    commit=False,
                )
                memory_id = _require_positive_int(placeholder.id, message_key=ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                if not await memory_job_crud.assign_create_memory_id(
                    db,
                    uid=claim.uid,
                    job_id=job_id,
                    memory_id=memory_id,
                    owner=context.worker_id,
                    commit=False,
                ):
                    current = await memory_job_crud.get_active_claim(
                        db,
                        uid=claim.uid,
                        job_id=job_id,
                        owner=context.worker_id,
                    )
                    if current is None:
                        raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            else:
                memory_id = _require_positive_int(claim.memory_id, message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID)
                placeholder = await memory_record_crud.get_by_id(db, uid=claim.uid, memory_id=memory_id)
                _validate_replacement_placeholder(
                    placeholder,
                    uid=claim.uid,
                    job_id=job_id,
                    memory_id=memory_id,
                )
            await _validate_unique_publication(db, uid=claim.uid, memory_id=memory_id, payload=publication)

            runtime_config = await load_embedding_runtime_config(
                db,
                store_snapshot.active_embedding_channel_id,
                store_snapshot.active_embedding_model_id,
            )
            updated_at_text = datetime.now(UTC).isoformat()
            await db.commit()
            return _MemoryReplacementSnapshot(
                uid=claim.uid,
                job_id=job_id,
                owner=context.worker_id,
                operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
                memory_id=memory_id,
                expected_version=None,
                publication=publication,
                candidate=candidate,
                store=store_snapshot,
                runtime_config=runtime_config,
                active_embedding_channel_id=store_snapshot.active_embedding_channel_id,
                active_embedding_model_id=store_snapshot.active_embedding_model_id,
                active_embedding_dimensions=store_snapshot.active_embedding_dimensions,
                active_embedding_signature=store_snapshot.active_embedding_signature,
                active_embedding_revision=store_snapshot.active_embedding_revision,
                active_collection_name=store_snapshot.active_collection_name,
                updated_at=updated_at_text,
            )
        except Exception:
            await db.rollback()
            raise


async def _prepare_organization_merge(context: MemoryJobExecutionContext) -> _MemoryOrganizationMergeSnapshot:
    checkpoint_job = await context.checkpoint()
    job_id = _require_job_id(checkpoint_job)
    async with context.session_factory() as db:
        try:
            claim = _validate_claim(
                context,
                await memory_job_crud.get_active_claim(
                    db,
                    uid=context.job.uid,
                    job_id=job_id,
                    owner=context.worker_id,
                ),
                LongTermMemoryMutationOperation.ORGANIZE_MERGE,
            )
            payload, source_payloads, target = _validate_organization_merge_payload(claim)
            await _validate_organization_merge_parent(db, job=claim, payload=payload)
            store = await memory_store_crud.lock_for_mutation(db, uid=claim.uid, commit=False)
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
            records = await memory_record_crud.get_organization_group(
                db,
                uid=claim.uid,
                memory_ids=[source["memory_id"] for source in source_payloads],
            )
            if [record.id for record in records] != [source["memory_id"] for source in source_payloads]:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            source_snapshots = tuple(
                _validate_organization_source_record(
                    record,
                    uid=claim.uid,
                    job_id=job_id,
                    source=source,
                )
                for record, source in zip(records, source_payloads, strict=True)
            )
            runtime_config = await _load_organization_runtime_config(
                db,
                channel_id=active_channel_id,
                model_id=active_model_id,
                dimensions=active_dimensions,
            )
            primary_source = next(source for source in source_snapshots if source.memory_id == payload["primary_memory_id"])
            updated_at_text = datetime.now(UTC).isoformat()
            await db.commit()
            return _MemoryOrganizationMergeSnapshot(
                uid=claim.uid,
                job_id=job_id,
                owner=context.worker_id,
                parent_job_id=payload["parent_job_id"],
                payload=payload,
                action=payload["action"],
                primary_memory_id=payload["primary_memory_id"],
                expected_version=primary_source.expected_version,
                target=target,
                sources=source_snapshots,
                runtime_config=runtime_config,
                active_embedding_channel_id=active_channel_id,
                active_embedding_model_id=active_model_id,
                active_embedding_dimensions=active_dimensions,
                active_embedding_signature=active_signature,
                active_embedding_revision=active_revision,
                active_collection_name=active_collection_name,
                index_revision=payload["index_revision"],
                previous_vector_item_id=primary_source.vector_item_id,
                updated_at=updated_at_text,
            )
        except Exception:
            await db.rollback()
            raise


async def _prepare_delete_cleanup(context: MemoryJobExecutionContext) -> _MemoryDeleteCleanupSnapshot:
    checkpoint_job = await context.checkpoint()
    job_id = _require_job_id(checkpoint_job)
    async with context.session_factory() as db:
        try:
            claim = _validate_claim(
                context,
                await memory_job_crud.get_active_claim(
                    db,
                    uid=context.job.uid,
                    job_id=job_id,
                    owner=context.worker_id,
                ),
                LongTermMemoryMutationOperation.DELETE_CLEANUP,
            )
            memory_id = _require_positive_int(claim.memory_id)
            expected_version = _require_non_negative_int(claim.expected_version)
            payload = _validate_delete_payload(claim, expected_version)
            active_key = _validate_active_key(
                claim,
                LongTermMemoryMutationOperation.DELETE_CLEANUP,
                claim.uid,
                payload,
                memory_id,
            )
            store = await memory_store_crud.lock_for_mutation(db, uid=claim.uid, commit=False)
            if store is None:
                raise _deterministic(ERR_MEMORY_NOT_CONFIGURED)
            _, _, _, _, _, collection_name, _ = _validate_active_store(store)
            organization_parent_job_id: int | None = None
            organization_merge_job_id: int | None = None
            merge_source: dict[str, Any] | None = None
            if payload["source"] == LongTermMemorySource.AUTO_ORGANIZE.value:
                (
                    organization_parent_job_id,
                    organization_merge_job_id,
                    merge_source,
                ) = await _validate_organization_cleanup_source(
                    db,
                    job=claim,
                    payload=payload,
                    memory_id=memory_id,
                    expected_version=expected_version,
                )
            elif claim.parent_job_id is not None:
                raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
            record = await memory_record_crud.get_by_id(db, uid=claim.uid, memory_id=memory_id)
            if record is None or record.pending_mutation_job_id != job_id or record.version != expected_version or record.is_active or record.deleted_at is None:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            if organization_parent_job_id is None:
                current_snapshot = build_memory_record_snapshot(record)
                if current_snapshot != payload["record_snapshot"]:
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            else:
                if record.memory_key is not None or record.content_hash is not None:
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                if record.pinned is not merge_source["pinned"]:
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                expected_snapshot = payload["record_snapshot"]
                if any(
                    (getattr(record, field) if field not in {"memory_type", "source"} else _enum_value(getattr(record, field))) != expected_snapshot[field]
                    for field in (
                        "content",
                        "content_token_count",
                        "memory_type",
                        "source",
                        "source_id",
                        "source_session_id",
                        "source_profile_id",
                        "source_message_id",
                        "source_job_id",
                        "change_evidence",
                        "version",
                    )
                ):
                    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
                current_snapshot = dict(expected_snapshot)
            await db.commit()
            return _MemoryDeleteCleanupSnapshot(
                uid=claim.uid,
                job_id=job_id,
                owner=context.worker_id,
                memory_id=memory_id,
                expected_version=expected_version,
                active_mutation_key=active_key,
                active_collection_name=collection_name,
                vector_item_id=getattr(record, "vector_item_id", None),
                record_snapshot=current_snapshot,
                organization_parent_job_id=organization_parent_job_id,
                organization_merge_job_id=organization_merge_job_id,
            )
        except Exception:
            await db.rollback()
            raise
