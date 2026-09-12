from __future__ import annotations

from typing import Any

from app.core.constants import (
    ERR_MEMORY_CAPACITY_EXCEEDED,
    ERR_MEMORY_CAPACITY_PENDING,
    ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_MAINTENANCE_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_OVER_LIMIT,
    ERR_MEMORY_RECORD_NOT_FOUND,
)
from app.core.crud.memory.store import (
    memory_record_crud,
)
from app.core.embedding.common import (
    EmbeddingRuntimeConfig,
    load_embedding_runtime_config,
)
from app.core.memory import (
    MemoryValidationError,
    build_memory_active_mutation_key,
    build_memory_record_snapshot,
)
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryRecordIndexStatus,
)

from .handler_contracts import (
    _deterministic,
    _MemoryOrganizationSourceSnapshot,
)
from .handler_replacement_validation import (
    _enum_value,
    _validate_active_store,
)

__all__ = []


def _validate_organization_store_snapshot(store: Any, payload: dict[str, Any]) -> tuple[int, str, int, str, int, str]:
    (
        active_channel_id,
        active_model_id,
        active_dimensions,
        active_signature,
        active_revision,
        active_collection_name,
        _,
    ) = _validate_active_store(store)
    try:
        index_status = LongTermMemoryIndexStatus(store.index_status)
        migration_status = LongTermMemoryMigrationStatus(store.migration_status) if store.migration_status is not None else None
    except (TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT) from exc
    if index_status != LongTermMemoryIndexStatus.READY or migration_status in {
        LongTermMemoryMigrationStatus.PREPARING,
        LongTermMemoryMigrationStatus.BUILDING,
        LongTermMemoryMigrationStatus.CATCHING_UP,
        LongTermMemoryMigrationStatus.VALIDATING,
        LongTermMemoryMigrationStatus.SWITCHING,
    }:
        raise _deterministic(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
    if active_revision != payload["active_embedding_revision"] or store.index_revision != payload["index_revision"]:
        raise _deterministic(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
    return (
        active_channel_id,
        active_model_id,
        active_dimensions,
        active_signature,
        active_revision,
        active_collection_name,
    )


def _validate_organization_source_record(
    record: Any,
    *,
    uid: str,
    job_id: int,
    source: dict[str, Any],
    expected_snapshot: dict[str, Any] | None = None,
) -> _MemoryOrganizationSourceSnapshot:
    memory_id = source["memory_id"]
    expected_version = source["expected_version"]
    pinned = source["pinned"]
    if (
        record is None
        or record.uid != uid
        or record.id != memory_id
        or record.is_active is not True
        or record.deleted_at is not None
        or _enum_value(record.index_status) != LongTermMemoryRecordIndexStatus.READY.value
        or record.indexed_version != expected_version
        or record.version != expected_version
        or not isinstance(record.vector_item_id, str)
        or not record.vector_item_id.strip()
        or record.suppress_recall is not False
        or record.pending_mutation_job_id != job_id
        or record.pinned is not pinned
    ):
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    try:
        record_snapshot = build_memory_record_snapshot(record)
    except (MemoryValidationError, TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    if record_snapshot["version"] != expected_version or (expected_snapshot is not None and record_snapshot != expected_snapshot):
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    return _MemoryOrganizationSourceSnapshot(
        memory_id=memory_id,
        expected_version=expected_version,
        pinned=pinned,
        vector_item_id=record.vector_item_id,
        record_snapshot=dict(record_snapshot),
    )


async def _load_organization_runtime_config(
    db: Any,
    *,
    channel_id: int,
    model_id: str,
    dimensions: int,
) -> EmbeddingRuntimeConfig:
    runtime_config = await load_embedding_runtime_config(db, channel_id, model_id)
    if runtime_config.channel_id != channel_id or runtime_config.model_id != model_id or (runtime_config.embedding_dimensions is not None and runtime_config.embedding_dimensions != dimensions):
        raise _deterministic(ERR_MEMORY_NOT_CONFIGURED)
    return runtime_config


def _validate_active_key(
    job: LongTermMemoryMutationJob,
    operation: LongTermMemoryMutationOperation,
    uid: str,
    payload: dict[str, Any],
    memory_id: int | None,
) -> str:
    if operation == LongTermMemoryMutationOperation.CREATE:
        active_key = build_memory_active_mutation_key(uid, memory_key=payload["memory_key"])
    else:
        if memory_id is None:
            raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        active_key = build_memory_active_mutation_key(uid, memory_id=memory_id)
    if job.active_mutation_key != active_key:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    return active_key


def _validate_record_state(
    record: Any,
    *,
    operation: LongTermMemoryMutationOperation,
    job_id: int,
    expected_version: int,
    payload: dict[str, Any],
) -> None:
    if record is None:
        raise _deterministic(ERR_MEMORY_RECORD_NOT_FOUND)
    if record.pending_mutation_job_id != job_id or record.version != expected_version:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    if operation == LongTermMemoryMutationOperation.CREATE:
        if record.version != 0 or record.is_active or record.deleted_at is not None:
            raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if record.memory_key is not None or record.content != "" or record.content_token_count != 0 or record.content_hash is not None or record.vector_item_id is not None or record.indexed_version != 0 or record.index_status != LongTermMemoryRecordIndexStatus.PENDING:
            raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        return
    if operation == LongTermMemoryMutationOperation.UPDATE:
        if not record.is_active or record.deleted_at is not None:
            raise _deterministic(ERR_MEMORY_RECORD_NOT_FOUND)
        if payload["suppress_current"] and (not record.suppress_recall or record.suppressed_by_job_id != job_id):
            raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        return
    raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)


def _validate_publication_capacity(
    capacity: Any,
    *,
    operation: LongTermMemoryMutationOperation,
    record: Any,
    payload: dict[str, Any],
    max_active_records: int,
) -> None:
    if capacity.is_over_limit:
        if operation == LongTermMemoryMutationOperation.UPDATE and payload["content_token_count"] < record.content_token_count:
            return
        raise _deterministic(ERR_MEMORY_OVER_LIMIT)

    if operation == LongTermMemoryMutationOperation.CREATE:
        if capacity.active_count >= max_active_records:
            raise _deterministic(ERR_MEMORY_CAPACITY_EXCEEDED, maximum=max_active_records)
        if capacity.occupied_count > max_active_records:
            raise _deterministic(ERR_MEMORY_CAPACITY_PENDING)
        return


async def _validate_unique_publication(
    db: Any,
    *,
    uid: str,
    memory_id: int,
    payload: dict[str, Any],
) -> None:
    key_record = await memory_record_crud.get_by_key(db, uid=uid, memory_key=payload["memory_key"])
    hash_record = await memory_record_crud.get_by_content_hash(db, uid=uid, content_hash=payload["content_hash"])
    if (key_record is not None and key_record.id != memory_id) or (hash_record is not None and hash_record.id != memory_id):
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)


def _validate_replacement_placeholder(
    record: Any,
    *,
    uid: str,
    job_id: int,
    memory_id: int,
) -> None:
    if (
        record is None
        or record.uid != uid
        or record.id != memory_id
        or record.version != 0
        or record.indexed_version != 0
        or record.is_active
        or record.deleted_at is not None
        or record.memory_key is not None
        or record.content != ""
        or record.content_token_count != 0
        or record.content_hash is not None
        or record.vector_item_id is not None
        or record.pending_mutation_job_id != job_id
        or _enum_value(record.index_status) != LongTermMemoryRecordIndexStatus.PENDING.value
    ):
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
