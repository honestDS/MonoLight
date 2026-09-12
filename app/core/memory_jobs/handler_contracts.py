from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
)
from app.core.embedding.common import (
    EmbeddingRuntimeConfig,
)
from app.core.i18n import t
from app.core.log import get_logger
from app.core.memory import (
    normalize_memory_publication_payload,
)
from app.core.memory_jobs.executor import (
    MemoryJobDeterministicError,
    MemoryJobExecutionContext,
    MemoryJobLeaseLostError,
    MemoryJobRetryableError,
)
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
)

__all__ = []

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

_ORGANIZATION_CLEANUP_PAYLOAD_FIELDS = frozenset(
    {
        "organization_parent_job_id",
        "organization_merge_job_id",
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

_ORGANIZATION_MERGE_PAYLOAD_FIELDS = frozenset(
    {
        "parent_job_id",
        "snapshot_digest",
        "active_embedding_revision",
        "index_revision",
        "policy_version",
        "action",
        "sources",
        "primary_memory_id",
        "target",
    }
)

_ORGANIZATION_MERGE_SOURCE_FIELDS = frozenset({"memory_id", "expected_version", "pinned"})

_ORGANIZATION_MERGE_TARGET_FIELDS = frozenset({"content", "memory_key", "memory_type", "content_token_count", "content_hash"})


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
    organization_parent_job_id: int | None
    organization_merge_job_id: int | None


@dataclass(frozen=True, slots=True)
class _MemoryOrganizationSourceSnapshot:
    memory_id: int
    expected_version: int
    pinned: bool
    vector_item_id: str
    record_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _MemoryOrganizationMergeSnapshot:
    uid: str
    job_id: int
    owner: str
    parent_job_id: int
    payload: dict[str, Any]
    action: str
    primary_memory_id: int
    expected_version: int
    target: dict[str, Any]
    sources: tuple[_MemoryOrganizationSourceSnapshot, ...]
    runtime_config: EmbeddingRuntimeConfig
    active_embedding_channel_id: int
    active_embedding_model_id: str
    active_embedding_dimensions: int
    active_embedding_signature: str
    active_embedding_revision: int
    active_collection_name: str
    index_revision: int
    previous_vector_item_id: str
    updated_at: str


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
    if set(payload) != allowed_fields:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    _validate_payload_source_fields(payload, job)
    if operation == LongTermMemoryMutationOperation.UPDATE:
        if not isinstance(payload.get("suppress_current"), bool):
            raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return payload
