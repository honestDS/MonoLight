"""Backfill delete snapshots and remove fully-cleared memory tombstones."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.memory.errors import MemoryValidationError
from app.core.memory.normalization import build_memory_record_snapshot, normalize_memory_record_snapshot
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryRecord,
    LongTermMemoryRevision,
)

MIGRATION_ID = "20260805_hard_delete_memory_tombstones_v1"


def _normalize_record_snapshot(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        return normalize_memory_record_snapshot(value)
    except (MemoryValidationError, KeyError, TypeError, ValueError):
        return None


def _is_valid_record_snapshot(value: object) -> bool:
    return _normalize_record_snapshot(value) is not None


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _job_lookup_value(
    job: LongTermMemoryMutationJob,
    payload: dict[str, Any],
    field: str,
) -> Any:
    value = getattr(job, field, None)
    return value if value is not None else payload.get(field)


def _expected_version(job: LongTermMemoryMutationJob, payload: dict[str, Any]) -> Any:
    value = _job_lookup_value(job, payload, "expected_version")
    return value if value is not None else payload.get("version")


def _snapshot_for_job(
    value: object,
    expected_version: Any,
) -> dict[str, Any] | None:
    snapshot = _normalize_record_snapshot(value)
    if snapshot is None:
        return None
    if expected_version is not None and snapshot["version"] != expected_version:
        return None
    return snapshot


def _built_snapshot(value: object, expected_version: Any) -> dict[str, Any] | None:
    try:
        snapshot = build_memory_record_snapshot(value)
    except (MemoryValidationError, KeyError, TypeError, ValueError):
        return None
    return _snapshot_for_job(snapshot, expected_version)


async def _find_record_snapshot(
    session: AsyncSession,
    job: LongTermMemoryMutationJob,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    uid = _job_lookup_value(job, payload, "uid")
    memory_id = _job_lookup_value(job, payload, "memory_id")
    expected_version = _expected_version(job, payload)
    if uid is None or memory_id is None:
        return None

    record_result = await session.execute(
        select(LongTermMemoryRecord)
        .where(
            LongTermMemoryRecord.uid == uid,
            LongTermMemoryRecord.id == memory_id,
            LongTermMemoryRecord.is_active.is_(False),
            LongTermMemoryRecord.deleted_at.is_not(None),
        )
        .limit(1)
    )
    record = record_result.scalars().first()
    if record is not None and (expected_version is None or record.version == expected_version):
        snapshot = _built_snapshot(record, expected_version)
        if snapshot is not None:
            return snapshot

    if expected_version is None:
        return None

    revision_result = await session.execute(
        select(LongTermMemoryRevision)
        .where(
            LongTermMemoryRevision.uid == uid,
            LongTermMemoryRevision.memory_id == memory_id,
            LongTermMemoryRevision.version == expected_version,
        )
        .limit(1)
    )
    revision = revision_result.scalars().first()
    if revision is None:
        return None

    return _built_snapshot(revision, expected_version)


def _is_succeeded(job: LongTermMemoryMutationJob) -> bool:
    return getattr(job.status, "value", job.status) == "succeeded"


async def migrate(session: AsyncSession) -> None:
    jobs_result = await session.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.operation == "delete_cleanup"))
    jobs = jobs_result.scalars().all()

    cleanable_tombstones: set[tuple[str, int, int]] = set()
    for job in jobs:
        payload = _as_dict(job.payload)
        expected_version = _expected_version(job, payload)
        snapshot = _snapshot_for_job(payload.get("record_snapshot"), expected_version)
        if snapshot is None:
            snapshot = await _find_record_snapshot(session, job, payload)
            if snapshot is None:
                continue
        if payload.get("record_snapshot") != snapshot:
            new_payload = dict(payload)
            new_payload["record_snapshot"] = snapshot
            job.payload = new_payload

        if _is_succeeded(job):
            result = _as_dict(job.result)
            if result.get("record_snapshot") != snapshot:
                new_result = dict(result)
                new_result["record_snapshot"] = snapshot
                job.result = new_result
            memory_id = _job_lookup_value(job, payload, "memory_id")
            if isinstance(job.uid, str) and isinstance(memory_id, int) and not isinstance(memory_id, bool) and isinstance(expected_version, int) and not isinstance(expected_version, bool):
                cleanable_tombstones.add((job.uid, memory_id, expected_version))

    await session.flush()

    for uid, memory_id, expected_version in cleanable_tombstones:
        await session.execute(
            delete(LongTermMemoryRecord).where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.version == expected_version,
                LongTermMemoryRecord.is_active.is_(False),
                LongTermMemoryRecord.deleted_at.is_not(None),
                LongTermMemoryRecord.pending_mutation_job_id.is_(None),
                or_(
                    LongTermMemoryRecord.memory_key.is_(None),
                    LongTermMemoryRecord.memory_key == "",
                ),
                or_(
                    LongTermMemoryRecord.content_hash.is_(None),
                    LongTermMemoryRecord.content_hash == "",
                ),
                or_(
                    LongTermMemoryRecord.vector_item_id.is_(None),
                    LongTermMemoryRecord.vector_item_id == "",
                ),
                LongTermMemoryRecord.content == "",
                LongTermMemoryRecord.indexed_version == 0,
            )
        )
