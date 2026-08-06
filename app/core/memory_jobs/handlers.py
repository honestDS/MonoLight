from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from numbers import Real
from types import MappingProxyType
from typing import Any

from app.core.constants import (
    ERR_MEMORY_CAPACITY_EXCEEDED,
    ERR_MEMORY_CAPACITY_PENDING,
    ERR_MEMORY_EMBEDDING_VECTOR_INVALID,
    ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED,
    ERR_MEMORY_JOB_CANCELLATION_REQUESTED,
    ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED,
    ERR_MEMORY_JOB_EMBEDDING_FAILED,
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_PREPARATION_FAILED,
    ERR_MEMORY_JOB_PUBLICATION_FAILED,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_JOB_VECTOR_DIMENSION_INVALID,
    ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_OVER_LIMIT,
    ERR_MEMORY_RECORD_NOT_FOUND,
    ERR_MEMORY_VERSION_CONFLICT,
    MEMORY_MAX_ACTIVE_RECORDS,
    MEMORY_ORGANIZE_TRIGGER_RECORDS,
)
from app.core.crud.memory import (
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.embedding.common import (
    EmbeddingRuntimeConfig,
    embed_texts_with_config,
    load_embedding_runtime_config,
)
from app.core.i18n import t
from app.core.log import get_logger
from app.core.memory import (
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryValidationError,
    append_memory_embedding_delta,
    build_memory_active_mutation_key,
    build_memory_record_snapshot,
    build_memory_vector_item_id,
    normalize_memory_publication_payload,
    normalize_memory_record_snapshot,
)
from app.core.memory.capacity import load_memory_capacity_snapshot
from app.core.memory_jobs.executor import (
    Handler,
    MemoryJobCancelledError,
    MemoryJobDeterministicError,
    MemoryJobExecutionContext,
    MemoryJobExecutionError,
    MemoryJobExecutionResult,
    MemoryJobExecutor,
    MemoryJobLeaseLostError,
    MemoryJobRetryableError,
    SessionFactory,
)
from app.core.memory_jobs.maintenance_handlers import create_memory_maintenance_job_handlers
from app.core.memory_jobs.manager import memory_job_manager
from app.models.memory import (
    LongTermMemoryCapacityStatus,
    LongTermMemoryEmbeddingDeltaAction,
    LongTermMemoryIndexStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryRecordIndexStatus,
    LongTermMemorySource,
)
from app.providers.database import AsyncSessionLocal
from app.providers.database.time import get_database_time
from app.providers.vector import (
    async_delete_collection_items,
    async_get_or_create_collection,
    async_upsert_collection_items,
    async_validate_collection,
)

logger = get_logger(__name__)

_SOURCE_PAYLOAD_FIELDS = (
    ("source_session_id", "source_session_id"),
    ("source_profile_id", "source_profile_id"),
    ("source_message_id", "source_message_id"),
)
_PUBLICATION_PAYLOAD_FIELDS = frozenset(
    {
        "content",
        "memory_key",
        "memory_type",
        "change_evidence",
        "source",
        "source_id",
        "source_session_id",
        "source_profile_id",
        "source_message_id",
        "content_token_count",
        "content_hash",
    }
)
_DELETE_CLEANUP_PAYLOAD_FIELDS = frozenset(
    {
        "record_snapshot",
        "version",
        "source",
        "source_id",
        "source_session_id",
        "source_profile_id",
        "source_message_id",
    }
)
_REPLACEMENT_PAYLOAD_FIELDS = frozenset({"publication", "candidate", "store"})
_REPLACEMENT_CANDIDATE_FIELDS = frozenset({"memory_id", "version", "vector_item_id", "record_snapshot"})
_REPLACEMENT_STORE_FIELDS = frozenset(
    {
        "active_embedding_channel_id",
        "active_embedding_model_id",
        "active_embedding_dimensions",
        "active_embedding_signature",
        "active_embedding_revision",
        "active_collection_name",
        "max_active_records",
        "organize_trigger_records",
        "active_count",
        "index_revision",
        "index_status",
        "capacity_status",
    }
)
_MEMORY_RECORD_SNAPSHOT_FIELDS = frozenset(
    {
        "memory_key",
        "content",
        "content_token_count",
        "content_hash",
        "memory_type",
        "source",
        "source_id",
        "source_session_id",
        "source_profile_id",
        "source_message_id",
        "source_job_id",
        "change_evidence",
        "version",
    }
)


@dataclass(frozen=True, slots=True)
class _MemoryPublicationSnapshot:
    uid: str
    job_id: int
    owner: str
    operation: LongTermMemoryMutationOperation
    memory_id: int
    expected_version: int
    payload: dict[str, Any]
    runtime_config: EmbeddingRuntimeConfig
    active_embedding_channel_id: int
    active_embedding_model_id: str
    active_embedding_dimensions: int
    active_embedding_signature: str
    active_embedding_revision: int
    active_collection_name: str
    previous_vector_item_id: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class _MemoryDeleteCleanupSnapshot:
    uid: str
    job_id: int
    owner: str
    memory_id: int
    expected_version: int
    active_mutation_key: str
    active_collection_name: str
    vector_item_id: str | None
    record_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _MemoryReplacementCandidateSnapshot:
    memory_id: int
    version: int
    vector_item_id: str
    record_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _MemoryReplacementStoreSnapshot:
    active_embedding_channel_id: int
    active_embedding_model_id: str
    active_embedding_dimensions: int
    active_embedding_signature: str
    active_embedding_revision: int
    active_collection_name: str
    max_active_records: int
    organize_trigger_records: int
    active_count: int
    index_revision: int
    index_status: str
    capacity_status: str


@dataclass(frozen=True, slots=True)
class _MemoryReplacementSnapshot:
    uid: str
    job_id: int
    owner: str
    operation: LongTermMemoryMutationOperation
    memory_id: int
    expected_version: None
    publication: dict[str, Any]
    candidate: _MemoryReplacementCandidateSnapshot
    store: _MemoryReplacementStoreSnapshot
    runtime_config: EmbeddingRuntimeConfig
    active_embedding_channel_id: int
    active_embedding_model_id: str
    active_embedding_dimensions: int
    active_embedding_signature: str
    active_embedding_revision: int
    active_collection_name: str
    updated_at: str


def _deterministic(message_key: str, **kwargs: Any) -> MemoryJobDeterministicError:
    return MemoryJobDeterministicError(t(message_key, **kwargs))


def _retryable(message_key: str, **kwargs: Any) -> MemoryJobRetryableError:
    return MemoryJobRetryableError(t(message_key, **kwargs))


def _require_job_id(job: LongTermMemoryMutationJob) -> int:
    if isinstance(job.id, bool) or not isinstance(job.id, int) or job.id < 1:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return job.id


def _require_positive_int(value: Any, *, message_key: str = ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _deterministic(message_key)
    return value


def _require_non_negative_int(value: Any, *, message_key: str = ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _deterministic(message_key)
    return value


def _operation(value: Any) -> LongTermMemoryMutationOperation:
    try:
        return LongTermMemoryMutationOperation(value)
    except (TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc


def _payload(job: LongTermMemoryMutationJob) -> dict[str, Any]:
    if not isinstance(job.payload, dict):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if "uid" in job.payload:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return dict(job.payload)


def _validate_claim(
    context: MemoryJobExecutionContext,
    claim: LongTermMemoryMutationJob | None,
    operation: LongTermMemoryMutationOperation,
) -> LongTermMemoryMutationJob:
    if claim is None or claim.id != context.job.id or claim.uid != context.job.uid or claim.locked_by != context.worker_id:
        raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
    if _operation(claim.operation) != operation:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    return claim


def _validate_payload_source_fields(payload: dict[str, Any], job: LongTermMemoryMutationJob) -> None:
    for payload_field, job_field in _SOURCE_PAYLOAD_FIELDS:
        if payload.get(payload_field) != getattr(job, job_field):
            raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)


def _normalize_publication_for_job(
    job: LongTermMemoryMutationJob,
    operation: LongTermMemoryMutationOperation,
) -> dict[str, Any]:
    payload = normalize_memory_publication_payload(_payload(job))
    allowed_fields = _PUBLICATION_PAYLOAD_FIELDS
    if operation == LongTermMemoryMutationOperation.UPDATE:
        allowed_fields = _PUBLICATION_PAYLOAD_FIELDS | {"suppress_current"}
    elif operation == LongTermMemoryMutationOperation.RESTORE:
        allowed_fields = _PUBLICATION_PAYLOAD_FIELDS | {"restored_from_version"}
    if set(payload) != allowed_fields:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    _validate_payload_source_fields(payload, job)
    if operation == LongTermMemoryMutationOperation.UPDATE:
        if not isinstance(payload.get("suppress_current"), bool):
            raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    elif operation == LongTermMemoryMutationOperation.RESTORE:
        _require_positive_int(payload.get("restored_from_version"), message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return payload


def _validate_delete_payload(job: LongTermMemoryMutationJob, expected_version: int) -> dict[str, Any]:
    payload = _payload(job)
    if set(payload) != _DELETE_CLEANUP_PAYLOAD_FIELDS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if payload["version"] != expected_version:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    try:
        record_snapshot = normalize_memory_record_snapshot(payload["record_snapshot"])
    except (MemoryValidationError, KeyError, TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    if not isinstance(record_snapshot, Mapping) or record_snapshot.get("version") != expected_version:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    payload["record_snapshot"] = dict(record_snapshot)
    try:
        LongTermMemorySource(payload["source"])
    except (TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    source_id = payload["source_id"]
    if source_id is not None and (not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 255):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    source_session_id = payload["source_session_id"]
    if source_session_id is not None and (not isinstance(source_session_id, str) or not source_session_id.strip() or len(source_session_id) > 100):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    for value in (payload["source_profile_id"], payload["source_message_id"]):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    _validate_payload_source_fields(payload, job)
    return payload


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


async def _assert_memory_id_available(db: Any, memory_id: int) -> None:
    if await memory_record_crud.exists_by_global_id(db, memory_id=memory_id):
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
        if record.memory_key is not None or record.content != "" or record.content_token_count != 0 or record.content_hash is not None or record.indexed_version != 0 or record.index_status != LongTermMemoryRecordIndexStatus.PENDING:
            raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        return
    if operation == LongTermMemoryMutationOperation.UPDATE:
        if not record.is_active or record.deleted_at is not None:
            raise _deterministic(ERR_MEMORY_RECORD_NOT_FOUND)
        if payload["suppress_current"] and (not record.suppress_recall or record.suppressed_by_job_id != job_id):
            raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        return
    if operation == LongTermMemoryMutationOperation.RESTORE:
        if record.is_active and record.deleted_at is not None:
            raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        if not record.is_active and record.deleted_at is None:
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

    if operation == LongTermMemoryMutationOperation.RESTORE and not record.is_active:
        if capacity.active_count >= max_active_records:
            raise _deterministic(ERR_MEMORY_CAPACITY_EXCEEDED, maximum=max_active_records)
        if capacity.occupied_count >= max_active_records:
            raise _deterministic(ERR_MEMORY_CAPACITY_PENDING)


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
                    next_memory_id = await memory_record_crud.get_next_memory_id(db, minimum_id=job_id)
                    placeholder = await memory_record_crud.create_pending_placeholder(
                        db,
                        uid=uid,
                        job_id=job_id,
                        memory_id=next_memory_id,
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
            memory_id = _require_positive_int(claim.memory_id, message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID)
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
            await _assert_memory_id_available(db, memory_id)
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
            record = await memory_record_crud.get_by_id(db, uid=claim.uid, memory_id=memory_id)
            if record is None or record.pending_mutation_job_id != job_id or record.version != expected_version or record.is_active or record.deleted_at is None:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            current_snapshot = build_memory_record_snapshot(record)
            if current_snapshot != payload["record_snapshot"]:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
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
            )
        except Exception:
            await db.rollback()
            raise


def _collection_metadata(snapshot: _MemoryPublicationSnapshot | _MemoryReplacementSnapshot) -> dict[str, Any]:
    return {
        "memory_type": "long_term_memory",
        "uid_sha256": hashlib.sha256(snapshot.uid.encode("utf-8")).hexdigest(),
        "embedding_signature": snapshot.active_embedding_signature,
        "embedding_revision": snapshot.active_embedding_revision,
    }


def _validate_embedding_result(embeddings: Any, dimensions: int) -> list[float]:
    if not isinstance(embeddings, list) or len(embeddings) != 1:
        raise _deterministic(ERR_MEMORY_EMBEDDING_VECTOR_INVALID)
    vector = embeddings[0]
    if not isinstance(vector, list) or not vector:
        raise _deterministic(ERR_MEMORY_EMBEDDING_VECTOR_INVALID)
    if any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) for value in vector):
        raise _deterministic(ERR_MEMORY_EMBEDDING_VECTOR_INVALID)
    if len(vector) != dimensions:
        raise _deterministic(ERR_MEMORY_JOB_VECTOR_DIMENSION_INVALID)
    return [float(value) for value in vector]


def _build_vector_metadata(snapshot: _MemoryPublicationSnapshot | _MemoryReplacementSnapshot, version: int) -> dict[str, Any]:
    payload = snapshot.payload if isinstance(snapshot, _MemoryPublicationSnapshot) else snapshot.publication
    metadata: dict[str, Any] = {
        "memory_id": snapshot.memory_id,
        "uid": snapshot.uid,
        "memory_key": payload["memory_key"],
        "memory_type": payload["memory_type"],
        "version": version,
        "source": payload["source"],
        "embedding_revision": snapshot.active_embedding_revision,
        "updated_at": snapshot.updated_at,
    }
    return metadata


async def _best_effort_delete_item(uid: str, job_id: int, collection_name: str, item_id: str, message_key: str) -> None:
    try:
        validation = await async_validate_collection(collection_name)
        if not getattr(validation, "exists", False):
            return
        await async_delete_collection_items(collection_name, [item_id], batch_size=1)
    except Exception as exc:
        logger.bind(
            uid=uid,
            job_id=job_id,
            item_id=item_id,
            exception_type=type(exc).__name__,
        ).warning(t(message_key))


async def _shield_best_effort_delete_item(
    uid: str,
    job_id: int,
    collection_name: str,
    item_id: str,
    message_key: str,
) -> None:
    cleanup_task = asyncio.create_task(_best_effort_delete_item(uid, job_id, collection_name, item_id, message_key))
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await asyncio.gather(cleanup_task, return_exceptions=True)
            raise
        except Exception:
            pass
        raise
    except Exception:
        await asyncio.gather(cleanup_task, return_exceptions=True)


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
            await _assert_memory_id_available(db, memory_id)
            await _validate_unique_publication(db, uid=snapshot.uid, memory_id=memory_id, payload=publication)
            if claim.cancel_requested_at is not None:
                raise MemoryJobCancelledError(t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED))

            now = await get_database_time(db)
            new_record = await memory_record_crud.create(
                db,
                uid=snapshot.uid,
                id=memory_id,
                memory_key=publication["memory_key"],
                memory_type=publication["memory_type"],
                content=publication["content"],
                content_token_count=publication["content_token_count"],
                content_hash=publication["content_hash"],
                version=1,
                indexed_version=1,
                vector_item_id=vector_item_id,
                source=publication["source"],
                source_id=publication["source_id"],
                source_session_id=publication["source_session_id"],
                source_profile_id=publication["source_profile_id"],
                source_message_id=publication["source_message_id"],
                source_job_id=snapshot.job_id,
                change_evidence=publication["change_evidence"],
                is_active=True,
                pinned=False,
                pending_mutation_job_id=None,
                suppress_recall=False,
                suppressed_by_job_id=None,
                index_status=LongTermMemoryRecordIndexStatus.READY,
                created_at=now,
                updated_at=now,
                indexed_at=now,
                deleted_at=None,
                commit=False,
            )
            if new_record.id != memory_id:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
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
                published_at=now,
                created_at=now,
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


async def _execute_publication(
    context: MemoryJobExecutionContext,
    operation: LongTermMemoryMutationOperation,
) -> MemoryJobExecutionResult:
    snapshot: _MemoryPublicationSnapshot | None = None
    item_id: str | None = None
    item_written = False
    published = False
    phase = "preparation"
    try:
        snapshot = await _prepare_publication(context, operation)
        await context.checkpoint()
        phase = "collection"
        await async_get_or_create_collection(
            snapshot.active_collection_name,
            metadata=_collection_metadata(snapshot),
            distance="cosine",
        )
        phase = "embedding"
        try:
            embeddings = await embed_texts_with_config(
                snapshot.runtime_config,
                [snapshot.payload["content"]],
                batch_size=1,
                dimensions=snapshot.active_embedding_dimensions,
            )
        except MemoryJobExecutionError:
            raise
        except Exception as exc:
            raise _retryable(ERR_MEMORY_JOB_EMBEDDING_FAILED) from exc
        vector = _validate_embedding_result(embeddings, snapshot.active_embedding_dimensions)

        await context.checkpoint()
        next_version = snapshot.expected_version + 1
        item_id = build_memory_vector_item_id(snapshot.memory_id, next_version)
        metadata = _build_vector_metadata(snapshot, next_version)
        phase = "vector_write"
        item_written = True
        try:
            await async_upsert_collection_items(
                snapshot.active_collection_name,
                [item_id],
                [snapshot.payload["content"]],
                [vector],
                [metadata],
                batch_size=1,
            )
        except MemoryJobExecutionError:
            raise
        except Exception as exc:
            raise _retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc

        await context.checkpoint()
        phase = "publication"
        execution_result = await _publish_version(context, snapshot, item_id)
        published = True
        if snapshot.previous_vector_item_id and snapshot.previous_vector_item_id != item_id:
            await _shield_best_effort_delete_item(
                snapshot.uid,
                snapshot.job_id,
                snapshot.active_collection_name,
                snapshot.previous_vector_item_id,
                ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            )
        return execution_result
    except MemoryJobExecutionError:
        raise
    except (MemoryValidationError, MemoryConflictError, MemoryNotFoundError) as exc:
        if isinstance(exc, MemoryValidationError) or phase == "preparation":
            raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    except Exception as exc:
        message_key = {
            "preparation": ERR_MEMORY_JOB_PREPARATION_FAILED,
            "collection": ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            "embedding": ERR_MEMORY_JOB_EMBEDDING_FAILED,
            "vector_write": ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            "publication": ERR_MEMORY_JOB_PUBLICATION_FAILED,
        }.get(phase, ERR_MEMORY_JOB_PUBLICATION_FAILED)
        logger.bind(
            uid=context.job.uid,
            job_id=context.job.id,
            exception_type=type(exc).__name__,
        ).warning(t(message_key))
        raise _retryable(message_key) from exc
    finally:
        if item_written and not published and snapshot is not None and item_id is not None:
            await _shield_best_effort_delete_item(
                snapshot.uid,
                snapshot.job_id,
                snapshot.active_collection_name,
                item_id,
                ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            )


async def _execute_replacement(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    snapshot: _MemoryReplacementSnapshot | None = None
    item_id: str | None = None
    item_written = False
    published = False
    phase = "preparation"
    try:
        snapshot = await _prepare_replacement(context)
        await context.checkpoint()
        phase = "collection"
        await async_get_or_create_collection(
            snapshot.active_collection_name,
            metadata=_collection_metadata(snapshot),
            distance="cosine",
        )
        phase = "embedding"
        try:
            embeddings = await embed_texts_with_config(
                snapshot.runtime_config,
                [snapshot.publication["content"]],
                batch_size=1,
                dimensions=snapshot.active_embedding_dimensions,
            )
        except MemoryJobExecutionError:
            raise
        except Exception as exc:
            raise _retryable(ERR_MEMORY_JOB_EMBEDDING_FAILED) from exc
        vector = _validate_embedding_result(embeddings, snapshot.active_embedding_dimensions)

        await context.checkpoint()
        item_id = build_memory_vector_item_id(snapshot.memory_id, 1)
        metadata = _build_vector_metadata(snapshot, 1)
        phase = "vector_write"
        item_written = True
        try:
            await async_upsert_collection_items(
                snapshot.active_collection_name,
                [item_id],
                [snapshot.publication["content"]],
                [vector],
                [metadata],
                batch_size=1,
            )
        except MemoryJobExecutionError:
            raise
        except Exception as exc:
            raise _retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc

        await context.checkpoint()
        phase = "publication"
        execution_result = await _publish_replacement(context, snapshot, item_id)
        published = True
        return execution_result
    except MemoryJobExecutionError:
        raise
    except (MemoryValidationError, MemoryConflictError, MemoryNotFoundError) as exc:
        if isinstance(exc, MemoryValidationError) or phase == "preparation":
            raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    except Exception as exc:
        message_key = {
            "preparation": ERR_MEMORY_JOB_PREPARATION_FAILED,
            "collection": ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            "embedding": ERR_MEMORY_JOB_EMBEDDING_FAILED,
            "vector_write": ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            "publication": ERR_MEMORY_JOB_PUBLICATION_FAILED,
        }.get(phase, ERR_MEMORY_JOB_PUBLICATION_FAILED)
        logger.bind(
            uid=context.job.uid,
            job_id=context.job.id,
            exception_type=type(exc).__name__,
        ).warning(t(message_key))
        raise _retryable(message_key) from exc
    finally:
        if item_written and not published and snapshot is not None and item_id is not None:
            await _shield_best_effort_delete_item(
                snapshot.uid,
                snapshot.job_id,
                snapshot.active_collection_name,
                item_id,
                ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            )


async def _finalize_delete_cleanup(
    context: MemoryJobExecutionContext,
    snapshot: _MemoryDeleteCleanupSnapshot,
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
                LongTermMemoryMutationOperation.DELETE_CLEANUP,
            )
            memory_id = _require_positive_int(claim.memory_id)
            expected_version = _require_non_negative_int(claim.expected_version)
            payload = _validate_delete_payload(claim, expected_version)
            if memory_id != snapshot.memory_id or expected_version != snapshot.expected_version:
                raise _deterministic(ERR_MEMORY_VERSION_CONFLICT)
            if claim.active_mutation_key != snapshot.active_mutation_key:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            if payload["record_snapshot"] != snapshot.record_snapshot:
                raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
            record = await memory_record_crud.get_by_id(db, uid=snapshot.uid, memory_id=memory_id)
            if record is None or record.pending_mutation_job_id != snapshot.job_id or record.version != expected_version or record.is_active or record.deleted_at is None:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            if build_memory_record_snapshot(record) != snapshot.record_snapshot:
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            if not await memory_record_crud.delete_tombstone_after_cleanup(
                db,
                uid=snapshot.uid,
                memory_id=memory_id,
                job_id=snapshot.job_id,
                expected_version=expected_version,
                commit=False,
            ):
                raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
            result = {
                "memory_id": memory_id,
                "version": expected_version,
                "vector_item_id": snapshot.vector_item_id,
                "operation": LongTermMemoryMutationOperation.DELETE_CLEANUP.value,
                "record_snapshot": snapshot.record_snapshot,
            }
            if not await memory_job_crud.mark_succeeded(
                db,
                uid=snapshot.uid,
                job_id=snapshot.job_id,
                owner=snapshot.owner,
                result=result,
                commit=False,
            ):
                if (
                    await memory_job_crud.get_active_claim(
                        db,
                        uid=snapshot.uid,
                        job_id=snapshot.job_id,
                        owner=snapshot.owner,
                    )
                    is None
                ):
                    raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
                raise _retryable(ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED)
            await db.commit()
            return MemoryJobExecutionResult(result=result, finalized=True)
        except Exception:
            await db.rollback()
            raise


async def _handle_delete_cleanup(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    snapshot: _MemoryDeleteCleanupSnapshot | None = None
    phase = "preparation"
    try:
        snapshot = await _prepare_delete_cleanup(context)
        await context.checkpoint()
        if snapshot.vector_item_id:
            phase = "delete_cleanup"
            try:
                validation = await async_validate_collection(snapshot.active_collection_name)
                if getattr(validation, "exists", False):
                    await async_delete_collection_items(
                        snapshot.active_collection_name,
                        [snapshot.vector_item_id],
                        batch_size=1,
                    )
            except MemoryJobExecutionError:
                raise
            except Exception as exc:
                raise _retryable(ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED) from exc
        phase = "publication"
        return await _finalize_delete_cleanup(context, snapshot)
    except MemoryJobExecutionError:
        raise
    except (MemoryValidationError, MemoryConflictError, MemoryNotFoundError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    except Exception as exc:
        message_key = ERR_MEMORY_JOB_PREPARATION_FAILED if phase == "preparation" else ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED
        logger.bind(
            uid=context.job.uid,
            job_id=context.job.id,
            exception_type=type(exc).__name__,
        ).warning(t(message_key))
        raise _retryable(message_key) from exc


async def _handle_create(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    return await _execute_publication(context, LongTermMemoryMutationOperation.CREATE)


async def _handle_create_with_eviction(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    return await _execute_replacement(context)


async def _handle_update(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    return await _execute_publication(context, LongTermMemoryMutationOperation.UPDATE)


async def _handle_restore(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    return await _execute_publication(context, LongTermMemoryMutationOperation.RESTORE)


def create_memory_job_handlers() -> Mapping[LongTermMemoryMutationOperation, Handler]:
    handlers = {
        **create_memory_maintenance_job_handlers(),
        LongTermMemoryMutationOperation.CREATE: _handle_create,
        LongTermMemoryMutationOperation.CREATE_WITH_EVICTION: _handle_create_with_eviction,
        LongTermMemoryMutationOperation.UPDATE: _handle_update,
        LongTermMemoryMutationOperation.RESTORE: _handle_restore,
        LongTermMemoryMutationOperation.DELETE_CLEANUP: _handle_delete_cleanup,
    }
    return MappingProxyType(handlers)


def create_default_memory_job_executor(session_factory: SessionFactory = AsyncSessionLocal) -> MemoryJobExecutor:
    return MemoryJobExecutor(create_memory_job_handlers(), session_factory=session_factory)


default_memory_job_executor = create_default_memory_job_executor()


__all__ = [
    "create_default_memory_job_executor",
    "create_memory_job_handlers",
    "default_memory_job_executor",
]
