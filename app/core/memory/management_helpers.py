from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import uuid4

from app.core.constants import (
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_VALUE_MUST_BE_BETWEEN,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
)
from app.core.memory.errors import MemoryConflictError, MemoryValidationError
from app.core.memory.normalization import _normalize_enum, _require_non_negative, _require_positive
from app.core.memory_jobs.maintenance_state import validate_migration_payload
from app.core.memory_jobs.manager import MemoryJobSubmissionResult
from app.models.memory import (
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationStatus,
    LongTermMemoryOldCollectionCleanupStatus,
    LongTermMemoryRecord,
    LongTermMemoryStore,
)

_ACTIVE_MIGRATION_STATUSES = frozenset(
    {
        LongTermMemoryMigrationStatus.PREPARING,
        LongTermMemoryMigrationStatus.BUILDING,
        LongTermMemoryMigrationStatus.CATCHING_UP,
        LongTermMemoryMigrationStatus.VALIDATING,
        LongTermMemoryMigrationStatus.SWITCHING,
    }
)
_TERMINAL_JOB_STATUSES = frozenset(
    {
        LongTermMemoryMutationStatus.FAILED,
        LongTermMemoryMutationStatus.CANCELLED,
    }
)
_BLOCKING_CLEANUP_STATUSES = frozenset(
    {
        LongTermMemoryOldCollectionCleanupStatus.PENDING,
        LongTermMemoryOldCollectionCleanupStatus.RUNNING,
        LongTermMemoryOldCollectionCleanupStatus.FAILED,
    }
)


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items() if key != "uid"}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _model_view(model: Any) -> dict[str, Any]:
    if model is None:
        return {}
    return _json_value(model.model_dump(exclude={"uid"}))


def _record_view(record: LongTermMemoryRecord | None) -> dict[str, Any] | None:
    return _model_view(record) if record is not None else None


def _job_view(job: LongTermMemoryMutationJob | None) -> dict[str, Any] | None:
    return _model_view(job) if job is not None else None


def _revision_view(revision: LongTermMemoryEmbeddingRevision | None) -> dict[str, Any] | None:
    return _model_view(revision) if revision is not None else None


def _page(skip: int, limit: int) -> tuple[int, int]:
    normalized_skip = _require_non_negative(skip, field="skip", error_key=ERR_VALUE_MUST_BE_NON_NEGATIVE)
    normalized_limit = _require_positive(limit, field="limit")
    if normalized_limit > 100:
        raise MemoryValidationError(
            ERR_VALUE_MUST_BE_BETWEEN,
            params={"field": "limit", "minimum": 1, "maximum": 100},
        )
    return normalized_skip, normalized_limit


def _optional_enum(value: Any, enum_type: type[StrEnum], *, field: str) -> Any:
    return None if value is None else _normalize_enum(value, enum_type, field=field)


def _new_dedupe_key(job: LongTermMemoryMutationJob, *, prefix: str = "memory-retry") -> str:
    return f"{prefix}:{job.id}:{uuid4().hex}"


def _migration_status(store: LongTermMemoryStore | None) -> LongTermMemoryMigrationStatus | None:
    if store is None or store.migration_status is None:
        return None
    try:
        return LongTermMemoryMigrationStatus(store.migration_status)
    except (TypeError, ValueError):
        return None


def _store_progress_view(store: LongTermMemoryStore | None) -> dict[str, Any]:
    if store is None:
        return {}
    return {
        "active_embedding_channel_id": store.active_embedding_channel_id,
        "active_embedding_model_id": store.active_embedding_model_id,
        "active_embedding_dimensions": store.active_embedding_dimensions,
        "active_embedding_signature": store.active_embedding_signature,
        "active_embedding_revision": store.active_embedding_revision,
        "active_collection_name": store.active_collection_name,
        "target_embedding_channel_id": store.target_embedding_channel_id,
        "target_embedding_model_id": store.target_embedding_model_id,
        "target_embedding_dimensions": store.target_embedding_dimensions,
        "target_embedding_signature": store.target_embedding_signature,
        "target_collection_name": store.target_collection_name,
        "migration_job_id": store.migration_job_id,
        "migration_status": _json_value(store.migration_status),
        "migration_snapshot_boundary": store.migration_snapshot_boundary,
        "migration_cursor": store.migration_cursor,
        "migration_total_count": store.migration_total_count,
        "migration_success_count": store.migration_success_count,
        "migration_failure_count": store.migration_failure_count,
        "migration_delta_high_watermark": store.migration_delta_high_watermark,
        "migration_delta_applied_watermark": store.migration_delta_applied_watermark,
        "migration_error": store.migration_error,
        "migration_started_at": store.migration_started_at,
        "migration_finished_at": store.migration_finished_at,
        "old_collection_name": store.old_collection_name,
        "old_collection_cleanup_status": _json_value(store.old_collection_cleanup_status),
        "old_collection_cleanup_job_id": store.old_collection_cleanup_job_id,
        "old_collection_cleanup_error": store.old_collection_cleanup_error,
        "old_collection_cleanup_at": store.old_collection_cleanup_at,
        "index_revision": store.index_revision,
        "index_status": _json_value(store.index_status),
        "max_active_records": store.max_active_records,
        "organize_trigger_records": store.organize_trigger_records,
        "capacity_status": _json_value(store.capacity_status),
    }


def _migration_view(
    job: LongTermMemoryMutationJob,
    revision: LongTermMemoryEmbeddingRevision | None,
    store: LongTermMemoryStore | None,
) -> dict[str, Any]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    source = _json_value(payload.get("from")) if isinstance(payload.get("from"), dict) else None
    target = _json_value(payload.get("target")) if isinstance(payload.get("target"), dict) else None
    store_view = _store_progress_view(store)
    item: dict[str, Any] = {
        "id": job.id,
        "job_id": job.id,
        "revision": revision.revision if revision is not None else None,
        "status": _json_value(job.status),
        "job_status": _json_value(job.status),
        "migration_status": store_view.get("migration_status"),
        "from": source,
        "target": target,
        "embedding_revision": _revision_view(revision),
        "job": _job_view(job),
        "store": store_view,
        "error": job.error or (revision.error if revision is not None else None),
    }
    item.update(store_view)
    if isinstance(source, dict):
        item.update(
            {
                "from_embedding_channel_id": source.get("channel_id"),
                "from_embedding_model_id": source.get("model_id"),
                "from_embedding_dimensions": source.get("dimensions"),
                "from_embedding_signature": source.get("signature"),
                "from_collection_name": source.get("collection"),
                "from_embedding_revision": source.get("revision"),
            }
        )
    if isinstance(target, dict):
        item.update(
            {
                "target_embedding_channel_id": target.get("channel_id"),
                "target_embedding_model_id": target.get("model_id"),
                "target_embedding_dimensions": target.get("dimensions"),
                "target_embedding_signature": target.get("signature"),
                "target_collection_name": target.get("collection"),
                "target_embedding_revision": target.get("revision"),
            }
        )
    return item


def _submission_view(submission: MemoryJobSubmissionResult) -> dict[str, Any]:
    return {"job": _job_view(submission.job), "created": submission.created}


def _mutation_view(result: Any) -> dict[str, Any]:
    return {"status": _json_value(result.status), "job": _job_view(result.job), "record": _record_view(result.record)}


def _cancel_view(result: Any) -> dict[str, Any]:
    return {"job": _job_view(result.job), "accepted": result.accepted, "changed": result.changed, "error": result.error}


def _validated_migration_payload(job: LongTermMemoryMutationJob) -> dict[str, Any]:
    try:
        return validate_migration_payload(job)
    except Exception as exc:
        raise MemoryConflictError(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc


def _active_store_matches(store: LongTermMemoryStore, source: dict[str, Any]) -> bool:
    return (
        store.active_embedding_channel_id == source["channel_id"]
        and store.active_embedding_model_id == source["model_id"]
        and store.active_embedding_dimensions == source["dimensions"]
        and store.active_embedding_signature == source["signature"]
        and store.active_embedding_revision == source["revision"]
        and store.active_collection_name == source["collection"]
    )


def _validate_management_active_store(store: LongTermMemoryStore) -> None:
    if (
        isinstance(store.active_embedding_channel_id, bool)
        or not isinstance(store.active_embedding_channel_id, int)
        or store.active_embedding_channel_id < 1
        or not isinstance(store.active_embedding_model_id, str)
        or not store.active_embedding_model_id
        or isinstance(store.active_embedding_dimensions, bool)
        or not isinstance(store.active_embedding_dimensions, int)
        or store.active_embedding_dimensions < 1
        or not isinstance(store.active_embedding_signature, str)
        or not store.active_embedding_signature
        or isinstance(store.active_embedding_revision, bool)
        or not isinstance(store.active_embedding_revision, int)
        or store.active_embedding_revision < 1
        or not isinstance(store.active_collection_name, str)
        or not store.active_collection_name
    ):
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)


__all__ = [
    "_ACTIVE_MIGRATION_STATUSES",
    "_BLOCKING_CLEANUP_STATUSES",
    "_TERMINAL_JOB_STATUSES",
    "_active_store_matches",
    "_cancel_view",
    "_json_value",
    "_job_view",
    "_migration_status",
    "_migration_view",
    "_model_view",
    "_mutation_view",
    "_new_dedupe_key",
    "_optional_enum",
    "_page",
    "_record_view",
    "_revision_view",
    "_store_progress_view",
    "_submission_view",
    "_validate_management_active_store",
    "_validated_migration_payload",
]
