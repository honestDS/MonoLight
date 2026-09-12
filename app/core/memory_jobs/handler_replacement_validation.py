from __future__ import annotations

from typing import Any

from app.core.constants import (
    ERR_MEMORY_CAPACITY_PENDING,
    ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_OVER_LIMIT,
    MEMORY_MAX_ACTIVE_RECORDS,
    MEMORY_ORGANIZE_TRIGGER_RECORDS,
)
from app.core.memory import (
    MemoryValidationError,
    build_memory_record_snapshot,
    normalize_memory_publication_payload,
    normalize_memory_record_snapshot,
)
from app.core.memory.capacity import load_memory_capacity_snapshot
from app.models.memory import (
    LongTermMemoryCapacityStatus,
    LongTermMemoryIndexStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryRecordIndexStatus,
)

from .handler_contracts import (
    _MEMORY_RECORD_SNAPSHOT_FIELDS,
    _PUBLICATION_PAYLOAD_FIELDS,
    _REPLACEMENT_CANDIDATE_FIELDS,
    _REPLACEMENT_PAYLOAD_FIELDS,
    _REPLACEMENT_STORE_FIELDS,
    _deterministic,
    _MemoryPublicationSnapshot,
    _MemoryReplacementCandidateSnapshot,
    _MemoryReplacementStoreSnapshot,
    _payload,
    _require_positive_int,
    _retryable,
    _validate_payload_source_fields,
)

__all__ = []


def _validate_replacement_candidate_payload(value: Any) -> _MemoryReplacementCandidateSnapshot:
    if not isinstance(value, dict) or set(value) != _REPLACEMENT_CANDIDATE_FIELDS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    memory_id = _require_positive_int(value.get("memory_id"), message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID)
    version = _require_positive_int(value.get("version"), message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID)
    vector_item_id = value.get("vector_item_id")
    if not isinstance(vector_item_id, str) or not vector_item_id:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    record_snapshot_value = value.get("record_snapshot")
    if not isinstance(record_snapshot_value, dict) or set(record_snapshot_value) != _MEMORY_RECORD_SNAPSHOT_FIELDS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    try:
        record_snapshot = normalize_memory_record_snapshot(record_snapshot_value)
    except (MemoryValidationError, KeyError, TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    if record_snapshot["version"] != version:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return _MemoryReplacementCandidateSnapshot(
        memory_id=memory_id,
        version=version,
        vector_item_id=vector_item_id,
        record_snapshot=dict(record_snapshot),
    )


def _validate_replacement_store_payload(value: Any) -> _MemoryReplacementStoreSnapshot:
    if not isinstance(value, dict) or set(value) != _REPLACEMENT_STORE_FIELDS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)

    channel_id = value.get("active_embedding_channel_id")
    model_id = value.get("active_embedding_model_id")
    dimensions = value.get("active_embedding_dimensions")
    signature = value.get("active_embedding_signature")
    embedding_revision = value.get("active_embedding_revision")
    collection_name = value.get("active_collection_name")
    max_active_records = value.get("max_active_records")
    organize_trigger_records = value.get("organize_trigger_records")
    active_count = value.get("active_count")
    index_revision = value.get("index_revision")
    if (
        isinstance(channel_id, bool)
        or not isinstance(channel_id, int)
        or channel_id < 1
        or not isinstance(model_id, str)
        or not model_id.strip()
        or isinstance(dimensions, bool)
        or not isinstance(dimensions, int)
        or dimensions < 1
        or not isinstance(signature, str)
        or not signature.strip()
        or isinstance(embedding_revision, bool)
        or not isinstance(embedding_revision, int)
        or embedding_revision < 1
        or not isinstance(collection_name, str)
        or not collection_name.strip()
        or isinstance(max_active_records, bool)
        or not isinstance(max_active_records, int)
        or max_active_records != MEMORY_MAX_ACTIVE_RECORDS
        or isinstance(organize_trigger_records, bool)
        or not isinstance(organize_trigger_records, int)
        or organize_trigger_records != MEMORY_ORGANIZE_TRIGGER_RECORDS
        or isinstance(active_count, bool)
        or not isinstance(active_count, int)
        or active_count != MEMORY_MAX_ACTIVE_RECORDS
        or isinstance(index_revision, bool)
        or not isinstance(index_revision, int)
        or index_revision < 0
    ):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)

    try:
        index_status = LongTermMemoryIndexStatus(value.get("index_status")).value
        capacity_status = LongTermMemoryCapacityStatus(value.get("capacity_status")).value
    except (TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    if index_status != LongTermMemoryIndexStatus.READY.value:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if capacity_status != LongTermMemoryCapacityStatus.NORMAL.value:
        raise _deterministic(ERR_MEMORY_OVER_LIMIT)
    return _MemoryReplacementStoreSnapshot(
        active_embedding_channel_id=channel_id,
        active_embedding_model_id=model_id,
        active_embedding_dimensions=dimensions,
        active_embedding_signature=signature,
        active_embedding_revision=embedding_revision,
        active_collection_name=collection_name,
        max_active_records=max_active_records,
        organize_trigger_records=organize_trigger_records,
        active_count=active_count,
        index_revision=index_revision,
        index_status=index_status,
        capacity_status=capacity_status,
    )


def _validate_replacement_payload(
    job: LongTermMemoryMutationJob,
) -> tuple[dict[str, Any], _MemoryReplacementCandidateSnapshot, _MemoryReplacementStoreSnapshot]:
    payload = _payload(job)
    if set(payload) != _REPLACEMENT_PAYLOAD_FIELDS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)

    raw_publication = payload.get("publication")
    if not isinstance(raw_publication, dict) or set(raw_publication) != _PUBLICATION_PAYLOAD_FIELDS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    try:
        publication = normalize_memory_publication_payload(raw_publication)
    except (MemoryValidationError, KeyError, TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    if set(publication) != _PUBLICATION_PAYLOAD_FIELDS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    _validate_payload_source_fields(publication, job)

    candidate = _validate_replacement_candidate_payload(payload.get("candidate"))
    store = _validate_replacement_store_payload(payload.get("store"))
    return publication, candidate, store


def _validate_replacement_candidate_record(
    record: Any,
    *,
    uid: str,
    job_id: int,
    candidate: _MemoryReplacementCandidateSnapshot,
) -> None:
    if (
        record is None
        or record.uid != uid
        or record.id != candidate.memory_id
        or not record.is_active
        or record.deleted_at is not None
        or record.suppress_recall
        or _enum_value(record.index_status) != LongTermMemoryRecordIndexStatus.READY.value
        or record.indexed_version != candidate.version
        or record.vector_item_id != candidate.vector_item_id
        or record.pinned
        or record.pending_mutation_job_id != job_id
        or record.version != candidate.version
    ):
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    try:
        current_snapshot = build_memory_record_snapshot(record)
    except (MemoryValidationError, TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    if current_snapshot != candidate.record_snapshot:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _validate_replacement_store_snapshot(
    store: Any,
    expected: _MemoryReplacementStoreSnapshot,
) -> None:
    _validate_active_store(store)
    try:
        actual_index_status = LongTermMemoryIndexStatus(store.index_status).value
        actual_capacity_status = LongTermMemoryCapacityStatus(store.capacity_status).value
    except (TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_NOT_CONFIGURED) from exc
    if actual_index_status != LongTermMemoryIndexStatus.READY.value:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    if actual_capacity_status == LongTermMemoryCapacityStatus.OVER_LIMIT.value:
        raise _deterministic(ERR_MEMORY_OVER_LIMIT)
    actual = (
        store.active_embedding_channel_id,
        store.active_embedding_model_id,
        store.active_embedding_dimensions,
        store.active_embedding_signature,
        store.active_embedding_revision,
        store.active_collection_name,
        store.max_active_records,
        store.organize_trigger_records,
        store.index_revision,
        actual_index_status,
        actual_capacity_status,
    )
    expected_values = (
        expected.active_embedding_channel_id,
        expected.active_embedding_model_id,
        expected.active_embedding_dimensions,
        expected.active_embedding_signature,
        expected.active_embedding_revision,
        expected.active_collection_name,
        expected.max_active_records,
        expected.organize_trigger_records,
        expected.index_revision,
        expected.index_status,
        expected.capacity_status,
    )
    if actual != expected_values:
        raise _retryable(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)


async def _validate_replacement_capacity(
    db: Any,
    *,
    uid: str,
    store: _MemoryReplacementStoreSnapshot,
) -> None:
    capacity = await load_memory_capacity_snapshot(db, uid, store.max_active_records)
    if capacity.is_over_limit:
        raise _deterministic(ERR_MEMORY_OVER_LIMIT)
    if capacity.pending_create_count != 0:
        raise _deterministic(ERR_MEMORY_CAPACITY_PENDING)
    if capacity.active_count != store.active_count or capacity.active_count != MEMORY_MAX_ACTIVE_RECORDS:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)


def _validate_active_store(store: Any) -> tuple[int, str, int, str, int, str, int]:
    channel_id = getattr(store, "active_embedding_channel_id", None)
    model_id = getattr(store, "active_embedding_model_id", None)
    dimensions = getattr(store, "active_embedding_dimensions", None)
    signature = getattr(store, "active_embedding_signature", None)
    revision = getattr(store, "active_embedding_revision", None)
    collection_name = getattr(store, "active_collection_name", None)
    max_active_records = getattr(store, "max_active_records", None)
    organize_trigger_records = getattr(store, "organize_trigger_records", None)
    if (
        isinstance(channel_id, bool)
        or not isinstance(channel_id, int)
        or channel_id < 1
        or not isinstance(model_id, str)
        or not model_id
        or isinstance(dimensions, bool)
        or not isinstance(dimensions, int)
        or dimensions < 1
        or not isinstance(signature, str)
        or not signature
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(collection_name, str)
        or not collection_name
        or isinstance(max_active_records, bool)
        or not isinstance(max_active_records, int)
        or max_active_records < 1
    ):
        raise _deterministic(ERR_MEMORY_NOT_CONFIGURED)
    if max_active_records > MEMORY_MAX_ACTIVE_RECORDS:
        raise _deterministic(ERR_MEMORY_OVER_LIMIT)
    if organize_trigger_records != MEMORY_ORGANIZE_TRIGGER_RECORDS:
        raise _deterministic(ERR_MEMORY_NOT_CONFIGURED)
    return channel_id, model_id, dimensions, signature, revision, collection_name, max_active_records


def _store_matches_snapshot(store: Any, snapshot: _MemoryPublicationSnapshot) -> None:
    values = (
        getattr(store, "active_embedding_channel_id", None),
        getattr(store, "active_embedding_model_id", None),
        getattr(store, "active_embedding_dimensions", None),
        getattr(store, "active_embedding_signature", None),
        getattr(store, "active_embedding_revision", None),
        getattr(store, "active_collection_name", None),
    )
    expected = (
        snapshot.active_embedding_channel_id,
        snapshot.active_embedding_model_id,
        snapshot.active_embedding_dimensions,
        snapshot.active_embedding_signature,
        snapshot.active_embedding_revision,
        snapshot.active_collection_name,
    )
    if values != expected:
        raise _retryable(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
