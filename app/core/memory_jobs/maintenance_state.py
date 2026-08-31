from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.constants import (
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
)
from app.core.crud.memory.maintenance import memory_maintenance_record_crud
from app.core.crud.memory.store import memory_store_crud
from app.core.embedding.common import (
    EmbeddingRuntimeConfig,
    load_embedding_runtime_config,
)
from app.core.i18n import t
from app.core.memory_jobs.executor import (
    MemoryJobDeterministicError,
    MemoryJobExecutionContext,
    MemoryJobLeaseLostError,
    MemoryJobRetryableError,
)
from app.models.memory import (
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryRecord,
)

BATCH_SIZE = 50
MAX_RECORD_ID = 2**63 - 1
REINDEX_OPERATION = LongTermMemoryMutationOperation.REINDEX
MIGRATION_OPERATION = LongTermMemoryMutationOperation.EMBEDDING_MIGRATION
MIGRATION_ACTIVE_STATUSES = frozenset(
    {
        LongTermMemoryMigrationStatus.PREPARING,
        LongTermMemoryMigrationStatus.BUILDING,
        LongTermMemoryMigrationStatus.CATCHING_UP,
        LongTermMemoryMigrationStatus.VALIDATING,
        LongTermMemoryMigrationStatus.SWITCHING,
    }
)
MIGRATION_PRE_SWITCH_STATUSES = MIGRATION_ACTIVE_STATUSES - {
    LongTermMemoryMigrationStatus.SWITCHING,
}
REINDEX_PHASES = frozenset({"preparing", "building", "validating", "switching"})
REINDEX_FROM_FIELDS = frozenset(
    {
        "channel_id",
        "model_id",
        "dimensions",
        "signature",
        "embedding_revision",
        "collection",
        "index_revision",
    }
)
REINDEX_TARGET_FIELDS = frozenset({"collection", "index_revision"})
REINDEX_PROGRESS_FIELDS = frozenset(
    {
        "phase",
        "snapshot_initialized",
        "snapshot_boundary",
        "cursor",
        "total_count",
        "success_count",
        "failure_count",
    }
)
MIGRATION_CONFIG_FIELDS = frozenset({"channel_id", "model_id", "dimensions", "signature", "collection", "revision"})


@dataclass(frozen=True, slots=True)
class RecordSnapshot:
    memory_id: int
    content: str
    vector_item_id: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ValidationSnapshot:
    records: tuple[RecordSnapshot, ...]
    count: int
    success_count: int


def deterministic(key: str) -> MemoryJobDeterministicError:
    return MemoryJobDeterministicError(t(key))


def retryable(key: str) -> MemoryJobRetryableError:
    return MemoryJobRetryableError(t(key))


def require_job_id(job: LongTermMemoryMutationJob) -> int:
    if isinstance(job.id, bool) or not isinstance(job.id, int) or job.id < 1:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return job.id


def positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_config_section(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    config = dict(value)
    if not positive_int(config["channel_id"]):
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if not isinstance(config["model_id"], str) or not config["model_id"].strip():
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if not positive_int(config["dimensions"]):
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if not isinstance(config["signature"], str) or not config["signature"].strip():
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if not isinstance(config["collection"], str) or not config["collection"].strip():
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    revision_field = "revision" if "revision" in fields else "embedding_revision"
    if revision_field == "revision":
        if not positive_int(config[revision_field]):
            raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    elif not non_negative_int(config[revision_field]):
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return config


def validate_reindex_payload(job: LongTermMemoryMutationJob) -> dict[str, Any]:
    payload = job.payload
    if not isinstance(payload, dict) or "uid" in payload or set(payload) != {"from", "target", "progress"}:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    source = validate_config_section(payload["from"], REINDEX_FROM_FIELDS)
    if not positive_int(source["embedding_revision"]):
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    target = payload["target"]
    if not isinstance(target, dict) or set(target) != REINDEX_TARGET_FIELDS:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if not isinstance(target["collection"], str) or not target["collection"].strip():
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if not non_negative_int(target["index_revision"]):
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if target["collection"] == source["collection"]:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if target["index_revision"] != source["index_revision"] + 1:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)

    progress = payload["progress"]
    if not isinstance(progress, dict) or set(progress) != REINDEX_PROGRESS_FIELDS:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if progress["phase"] not in REINDEX_PHASES or not isinstance(progress["snapshot_initialized"], bool):
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    numeric_fields = (
        "snapshot_boundary",
        "cursor",
        "total_count",
        "success_count",
        "failure_count",
    )
    for field in numeric_fields:
        if not non_negative_int(progress[field]):
            raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if progress["cursor"] > progress["snapshot_boundary"]:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if progress["success_count"] > progress["total_count"]:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if not progress["snapshot_initialized"]:
        if progress["phase"] != "preparing":
            raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
        for field in numeric_fields:
            if progress[field] != 0:
                raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return {
        "from": source,
        "target": dict(target),
        "progress": dict(progress),
    }


def validate_migration_payload(job: LongTermMemoryMutationJob) -> dict[str, Any]:
    payload = job.payload
    if not isinstance(payload, dict) or "uid" in payload or set(payload) != {"from", "target"}:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    source = validate_config_section(payload["from"], MIGRATION_CONFIG_FIELDS)
    target = validate_config_section(payload["target"], MIGRATION_CONFIG_FIELDS)
    if target["revision"] <= source["revision"]:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if target["collection"] == source["collection"]:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    source_embedding_config = (
        source["channel_id"],
        source["model_id"],
        source["dimensions"],
        source["signature"],
    )
    target_embedding_config = (
        target["channel_id"],
        target["model_id"],
        target["dimensions"],
        target["signature"],
    )
    same_embedding_config = source_embedding_config == target_embedding_config
    if same_embedding_config:
        raise deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return {"from": source, "target": target}


def validate_claim(
    context: MemoryJobExecutionContext,
    claim: LongTermMemoryMutationJob | None,
    operation: LongTermMemoryMutationOperation,
) -> LongTermMemoryMutationJob:
    if claim is None:
        raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
    if claim.id != context.job.id:
        raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
    if claim.uid != context.job.uid:
        raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
    if claim.locked_by != context.worker_id:
        raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
    if claim.operation != operation:
        raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    return claim


def validate_store_active(store: Any) -> None:
    channel_id = store.active_embedding_channel_id
    model_id = store.active_embedding_model_id
    dimensions = store.active_embedding_dimensions
    signature = store.active_embedding_signature
    embedding_revision = store.active_embedding_revision
    collection_name = store.active_collection_name
    index_revision = store.index_revision

    if not positive_int(channel_id):
        raise deterministic(ERR_MEMORY_NOT_CONFIGURED)
    if not isinstance(model_id, str) or not model_id.strip():
        raise deterministic(ERR_MEMORY_NOT_CONFIGURED)
    if not positive_int(dimensions):
        raise deterministic(ERR_MEMORY_NOT_CONFIGURED)
    if not isinstance(signature, str) or not signature.strip():
        raise deterministic(ERR_MEMORY_NOT_CONFIGURED)
    if not positive_int(embedding_revision):
        raise deterministic(ERR_MEMORY_NOT_CONFIGURED)
    if not isinstance(collection_name, str) or not collection_name.strip():
        raise deterministic(ERR_MEMORY_NOT_CONFIGURED)
    if not non_negative_int(index_revision):
        raise deterministic(ERR_MEMORY_NOT_CONFIGURED)


def matches_reindex_source(store: Any, source: dict[str, Any]) -> bool:
    return (
        store.active_embedding_channel_id == source["channel_id"]
        and store.active_embedding_model_id == source["model_id"]
        and store.active_embedding_dimensions == source["dimensions"]
        and store.active_embedding_signature == source["signature"]
        and store.active_embedding_revision == source["embedding_revision"]
        and store.active_collection_name == source["collection"]
        and store.index_revision == source["index_revision"]
    )


def matches_migration_source(store: Any, source: dict[str, Any]) -> bool:
    return (
        store.active_embedding_channel_id == source["channel_id"]
        and store.active_embedding_model_id == source["model_id"]
        and store.active_embedding_dimensions == source["dimensions"]
        and store.active_embedding_signature == source["signature"]
        and store.active_embedding_revision == source["revision"]
        and store.active_collection_name == source["collection"]
    )


def build_reindex_target_config(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    return {
        "channel_id": source["channel_id"],
        "model_id": source["model_id"],
        "dimensions": source["dimensions"],
        "signature": source["signature"],
        "embedding_revision": source["embedding_revision"],
        "collection": target["collection"],
        "index_revision": target["index_revision"],
    }


def build_migration_target_config(target: dict[str, Any], index_revision: int) -> dict[str, Any]:
    return {
        "channel_id": target["channel_id"],
        "model_id": target["model_id"],
        "dimensions": target["dimensions"],
        "signature": target["signature"],
        "embedding_revision": target["revision"],
        "collection": target["collection"],
        "index_revision": index_revision,
    }


def collection_metadata(uid: str, config: dict[str, Any], purpose: str) -> dict[str, Any]:
    return {
        "memory_type": "long_term_memory",
        "uid_sha256": hashlib.sha256(uid.encode("utf-8")).hexdigest(),
        "embedding_signature": config["signature"],
        "embedding_revision": config["embedding_revision"],
        "index_revision": config["index_revision"],
        "purpose": purpose,
        "hnsw:space": "cosine",
    }


def record_metadata(record: LongTermMemoryRecord, embedding_revision: int) -> dict[str, Any]:
    updated_at = record.updated_at
    if isinstance(updated_at, datetime):
        updated_at_value = updated_at.isoformat()
    else:
        updated_at_value = str(updated_at)
    metadata: dict[str, Any] = {
        "memory_id": record.id,
        "uid": record.uid,
        "memory_key": record.memory_key,
        "memory_type": getattr(record.memory_type, "value", record.memory_type),
        "version": record.version,
        "updated_at": updated_at_value,
        "source": getattr(record.source, "value", record.source),
        "embedding_revision": embedding_revision,
    }
    return metadata


def record_snapshot(record: LongTermMemoryRecord, embedding_revision: int) -> RecordSnapshot:
    if not positive_int(record.id) or not isinstance(record.vector_item_id, str) or not record.vector_item_id:
        raise deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
    return RecordSnapshot(
        memory_id=record.id,
        content=record.content,
        vector_item_id=record.vector_item_id,
        metadata=record_metadata(record, embedding_revision),
    )


async def read_store(context: MemoryJobExecutionContext) -> Any:
    async with context.session_factory() as db:
        store = await memory_store_crud.get_snapshot_by_uid(db, uid=context.job.uid)
        await db.commit()
        return store


async def load_runtime(
    context: MemoryJobExecutionContext,
    channel_id: int,
    model_id: str,
) -> EmbeddingRuntimeConfig:
    async with context.session_factory() as db:
        config = await load_embedding_runtime_config(db, channel_id, model_id)
        await db.commit()
        return config


async def read_recallable_records(
    db: Any,
    *,
    uid: str,
    boundary: int = MAX_RECORD_ID,
) -> list[LongTermMemoryRecord]:
    records: list[LongTermMemoryRecord] = []
    cursor = 0
    while True:
        page = await memory_maintenance_record_crud.list_recallable_page(
            db,
            uid=uid,
            after_id=cursor,
            boundary=boundary,
            limit=BATCH_SIZE,
        )
        if not page:
            return records
        records.extend(page)
        next_cursor = page[-1].id
        if not positive_int(next_cursor) or next_cursor <= cursor:
            raise retryable(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT)
        cursor = next_cursor


async def list_current_records(context: MemoryJobExecutionContext) -> list[LongTermMemoryRecord]:
    async with context.session_factory() as db:
        records = await read_recallable_records(db, uid=context.job.uid)
        await db.commit()
        return records


async def read_snapshot_page(
    context: MemoryJobExecutionContext,
    *,
    cursor: int,
    boundary: int,
) -> list[LongTermMemoryRecord]:
    async with context.session_factory() as db:
        records = await memory_maintenance_record_crud.list_recallable_page(
            db,
            uid=context.job.uid,
            after_id=cursor,
            boundary=boundary,
            limit=BATCH_SIZE,
        )
        await db.commit()
        return records


__all__ = [
    "BATCH_SIZE",
    "MAX_RECORD_ID",
    "MIGRATION_ACTIVE_STATUSES",
    "MIGRATION_OPERATION",
    "MIGRATION_PRE_SWITCH_STATUSES",
    "REINDEX_OPERATION",
    "RecordSnapshot",
    "ValidationSnapshot",
    "build_migration_target_config",
    "build_reindex_target_config",
    "collection_metadata",
    "deterministic",
    "list_current_records",
    "matches_migration_source",
    "matches_reindex_source",
    "non_negative_int",
    "positive_int",
    "read_recallable_records",
    "read_snapshot_page",
    "read_store",
    "record_metadata",
    "record_snapshot",
    "require_job_id",
    "retryable",
    "validate_claim",
    "validate_migration_payload",
    "validate_reindex_payload",
    "validate_store_active",
]
