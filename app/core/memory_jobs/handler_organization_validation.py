from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.i18n import t
from app.core.memory import (
    MemoryValidationError,
    build_memory_active_mutation_key,
    normalize_memory_publication_payload,
    normalize_memory_record_snapshot,
)
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemorySource,
)

from .handler_contracts import (
    _DELETE_CLEANUP_PAYLOAD_FIELDS,
    _ORGANIZATION_CLEANUP_PAYLOAD_FIELDS,
    _ORGANIZATION_MERGE_PAYLOAD_FIELDS,
    _ORGANIZATION_MERGE_SOURCE_FIELDS,
    _ORGANIZATION_MERGE_TARGET_FIELDS,
    _deterministic,
    _operation,
    _payload,
    _require_positive_int,
    _validate_payload_source_fields,
)

__all__ = []


def _validate_delete_payload(job: LongTermMemoryMutationJob, expected_version: int) -> dict[str, Any]:
    payload = _payload(job)
    if payload.get("source") == LongTermMemorySource.AUTO_ORGANIZE.value:
        allowed_fields = {
            _DELETE_CLEANUP_PAYLOAD_FIELDS,
            _DELETE_CLEANUP_PAYLOAD_FIELDS | _ORGANIZATION_CLEANUP_PAYLOAD_FIELDS,
        }
    else:
        allowed_fields = {_DELETE_CLEANUP_PAYLOAD_FIELDS}
    if set(payload) not in allowed_fields:
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
    if set(payload) == _DELETE_CLEANUP_PAYLOAD_FIELDS | _ORGANIZATION_CLEANUP_PAYLOAD_FIELDS:
        _require_positive_int(payload["organization_parent_job_id"], message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID)
        _require_positive_int(payload["organization_merge_job_id"], message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID)
    _validate_payload_source_fields(payload, job)
    return payload


def _validate_organization_merge_payload(
    job: LongTermMemoryMutationJob,
    *,
    allow_terminal_active_key: bool = False,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], dict[str, Any]]:
    payload = _payload(job)
    if set(payload) != _ORGANIZATION_MERGE_PAYLOAD_FIELDS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    try:
        parent_job_id = payload["parent_job_id"]
        snapshot_digest = payload["snapshot_digest"]
        active_embedding_revision = payload["active_embedding_revision"]
        index_revision = payload["index_revision"]
        policy_version = payload["policy_version"]
        action = payload["action"]
        primary_memory_id = payload["primary_memory_id"]
        raw_sources = payload["sources"]
        raw_target = payload["target"]
        if (
            not isinstance(parent_job_id, int)
            or isinstance(parent_job_id, bool)
            or parent_job_id < 1
            or job.parent_job_id != parent_job_id
            or not isinstance(snapshot_digest, str)
            or len(snapshot_digest) != 64
            or any(character not in "0123456789abcdef" for character in snapshot_digest)
            or not isinstance(active_embedding_revision, int)
            or isinstance(active_embedding_revision, bool)
            or active_embedding_revision < 1
            or not isinstance(index_revision, int)
            or isinstance(index_revision, bool)
            or index_revision < 0
            or not isinstance(policy_version, int)
            or isinstance(policy_version, bool)
            or policy_version < 1
            or action not in {"update", "merge"}
            or not isinstance(raw_sources, list)
            or not isinstance(primary_memory_id, int)
            or isinstance(primary_memory_id, bool)
            or primary_memory_id < 1
            or not isinstance(raw_target, dict)
            or set(raw_target) != _ORGANIZATION_MERGE_TARGET_FIELDS
        ):
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        source_by_id: dict[int, dict[str, Any]] = {}
        for raw_source in raw_sources:
            if set(raw_source) != _ORGANIZATION_MERGE_SOURCE_FIELDS:
                raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            memory_id = raw_source["memory_id"]
            expected_version = raw_source["expected_version"]
            pinned = raw_source["pinned"]
            if not isinstance(memory_id, int) or isinstance(memory_id, bool) or memory_id < 1 or not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 1 or not isinstance(pinned, bool) or memory_id in source_by_id:
                raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            source_by_id[memory_id] = {
                "memory_id": memory_id,
                "expected_version": expected_version,
                "pinned": pinned,
            }
        ordered_sources = tuple(source_by_id[memory_id] for memory_id in sorted(source_by_id))
        if list(raw_sources) != list(ordered_sources):
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if action == "update" and (len(ordered_sources) != 1 or primary_memory_id != ordered_sources[0]["memory_id"]):
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if action == "merge" and (len(ordered_sources) < 2 or primary_memory_id not in source_by_id):
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if sum(source["pinned"] for source in ordered_sources) > 1:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if any(source["pinned"] for source in ordered_sources) and not source_by_id[primary_memory_id]["pinned"]:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        raw_target_with_source = {
            "content": raw_target["content"],
            "memory_key": raw_target["memory_key"],
            "memory_type": raw_target["memory_type"],
            "change_evidence": None,
            "source": LongTermMemorySource.AUTO_ORGANIZE.value,
            "source_id": None,
            "source_session_id": None,
            "source_profile_id": None,
            "source_message_id": None,
            "content_token_count": raw_target["content_token_count"],
            "content_hash": raw_target["content_hash"],
        }
        normalized_target_with_source = normalize_memory_publication_payload(raw_target_with_source)
        normalized_target = {field: normalized_target_with_source[field] for field in _ORGANIZATION_MERGE_TARGET_FIELDS}
        if raw_target != normalized_target:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if (
            not isinstance(job.memory_id, int)
            or isinstance(job.memory_id, bool)
            or job.memory_id != primary_memory_id
            or not isinstance(job.expected_version, int)
            or isinstance(job.expected_version, bool)
            or job.expected_version != source_by_id[primary_memory_id]["expected_version"]
            or job.source_session_id is not None
            or job.source_profile_id is not None
            or job.source_message_id is not None
        ):
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        expected_active_key = build_memory_active_mutation_key(job.uid, memory_id=primary_memory_id)
        terminal_active_key = (
            allow_terminal_active_key
            and job.active_mutation_key is None
            and LongTermMemoryMutationStatus(job.status)
            in {
                LongTermMemoryMutationStatus.SUCCEEDED,
                LongTermMemoryMutationStatus.FAILED,
                LongTermMemoryMutationStatus.CANCELLED,
            }
        )
        if job.active_mutation_key != expected_active_key and not terminal_active_key:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
    except (MemoryValidationError, KeyError, TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc

    normalized_payload = {
        **payload,
        "sources": [dict(source) for source in ordered_sources],
        "target": dict(normalized_target),
    }
    return normalized_payload, ordered_sources, normalized_target


async def _validate_organization_merge_parent(
    db: Any,
    *,
    job: LongTermMemoryMutationJob,
    payload: dict[str, Any],
) -> None:
    parent_job = await memory_job_crud.get_by_id(
        db,
        uid=job.uid,
        job_id=payload["parent_job_id"],
    )
    if (
        parent_job is None
        or parent_job.uid != job.uid
        or parent_job.parent_job_id is not None
        or parent_job.memory_id is not None
        or parent_job.expected_version is not None
        or parent_job.source_session_id is not None
        or parent_job.source_profile_id is not None
        or parent_job.source_message_id is not None
        or _operation(parent_job.operation) != LongTermMemoryMutationOperation.ORGANIZE
    ):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    try:
        from app.core.memory.organization import restore_organization_execution_payload

        parent_payload = restore_organization_execution_payload(parent_job.payload)
    except Exception as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    if parent_payload.snapshot.digest != payload["snapshot_digest"] or parent_payload.snapshot.active_embedding_revision != payload["active_embedding_revision"] or parent_payload.snapshot.index_revision != payload["index_revision"] or parent_payload.snapshot.policy_version != payload["policy_version"]:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)


async def _validate_organization_cleanup_source(
    db: Any,
    *,
    job: LongTermMemoryMutationJob,
    payload: dict[str, Any],
    memory_id: int,
    expected_version: int,
) -> tuple[int, int, dict[str, Any]]:
    merge_job_id = _require_positive_int(job.parent_job_id, message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID)
    merge_job = await memory_job_crud.get_by_id(
        db,
        uid=job.uid,
        job_id=merge_job_id,
    )
    if merge_job is None or merge_job.uid != job.uid:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if _operation(merge_job.operation) != LongTermMemoryMutationOperation.ORGANIZE_MERGE:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    try:
        merge_status = LongTermMemoryMutationStatus(merge_job.status)
    except (TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    if merge_status != LongTermMemoryMutationStatus.SUCCEEDED:
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)

    merge_payload, merge_sources, _ = _validate_organization_merge_payload(
        merge_job,
        allow_terminal_active_key=True,
    )
    organization_parent_job_id = _require_positive_int(
        merge_payload["parent_job_id"],
        message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID,
    )
    payload_merge_job_id = payload.get("organization_merge_job_id", merge_job_id)
    payload_parent_job_id = payload.get("organization_parent_job_id", organization_parent_job_id)
    if _require_positive_int(payload_merge_job_id, message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID) != merge_job_id or _require_positive_int(payload_parent_job_id, message_key=ERR_MEMORY_JOB_PAYLOAD_INVALID) != organization_parent_job_id:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    await _validate_organization_merge_parent(
        db,
        job=merge_job,
        payload=merge_payload,
    )
    merge_source = next(
        (source for source in merge_sources if source["memory_id"] == memory_id),
        None,
    )
    if merge_source is None or memory_id == merge_payload["primary_memory_id"] or merge_source["expected_version"] != expected_version:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return organization_parent_job_id, merge_job_id, merge_source
