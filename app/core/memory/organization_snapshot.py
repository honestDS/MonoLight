from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_MAINTENANCE_STATE_CONFLICT,
    ERR_MEMORY_NOT_CONFIGURED,
)
from app.core.i18n import t
from app.core.memory.errors import MemoryConflictError
from app.core.memory.normalization import (
    _normalize_dedupe_key,
    _normalize_uid,
)
from app.core.memory.organization_types import (
    MemoryOrganizationSnapshotItem,
)
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryRecord,
    LongTermMemoryStore,
)

from .organization_contracts import (
    MemoryOrganizationModelConfig,
    MemoryOrganizationSnapshot,
    MemoryOrganizationValidatedItem,
)
from .organization_payload import (
    _ORGANIZATION_TRIGGERS,
    _raise_organization_payload_invalid,
)

__all__ = [
    "build_organization_snapshot_items",
    "build_organization_snapshot_digest",
    "build_organization_snapshot",
    "build_organization_dedupe_key",
    "build_organization_job_payload",
    "build_organization_merge_child_payload",
    "build_organization_merge_child_dedupe_key",
    "validate_organization_submission_store",
]


def build_organization_snapshot_items(
    records: Iterable[LongTermMemoryRecord],
) -> tuple[MemoryOrganizationSnapshotItem, ...]:
    return tuple(
        MemoryOrganizationSnapshotItem.model_validate(
            {
                "memory_id": record.id,
                "expected_version": record.version,
                "memory_key": record.memory_key,
                "memory_type": record.memory_type,
                "content": record.content,
                "content_token_count": record.content_token_count,
                "pinned": record.pinned,
            }
        )
        for record in sorted(records, key=lambda item: item.id or 0)
    )


def build_organization_snapshot_digest(
    items: Iterable[MemoryOrganizationSnapshotItem],
    *,
    active_embedding_revision: int,
    index_revision: int,
    policy_version: int,
) -> str:
    ordered_items = tuple(sorted(items, key=lambda item: item.memory_id))
    digest_payload = {
        "active_embedding_revision": active_embedding_revision,
        "index_revision": index_revision,
        "items": [item.model_dump(mode="json") for item in ordered_items],
        "policy_version": policy_version,
    }
    return hashlib.sha256(canonical_json_dumps(digest_payload).encode("utf-8")).hexdigest()


def build_organization_snapshot(
    records: Iterable[LongTermMemoryRecord],
    *,
    active_embedding_revision: int,
    index_revision: int,
    policy_version: int,
) -> MemoryOrganizationSnapshot:
    items = build_organization_snapshot_items(records)
    digest = build_organization_snapshot_digest(
        items,
        active_embedding_revision=active_embedding_revision,
        index_revision=index_revision,
        policy_version=policy_version,
    )
    return MemoryOrganizationSnapshot(
        digest=digest,
        count=len(items),
        active_embedding_revision=active_embedding_revision,
        index_revision=index_revision,
        policy_version=policy_version,
        items=items,
    )


def build_organization_dedupe_key(
    uid: str,
    *,
    snapshot_digest: str,
    policy_version: int,
    caller_dedupe_key: str | None = None,
) -> str:
    normalized_uid = _normalize_uid(uid)
    normalized_caller_key = _normalize_dedupe_key(caller_dedupe_key) if caller_dedupe_key is not None else None
    payload = {
        "caller_dedupe_key": normalized_caller_key,
        "policy_version": policy_version,
        "scope": "memory_organization",
        "snapshot_digest": snapshot_digest,
        "uid": normalized_uid,
    }
    digest = hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()
    return f"memory_organization:{digest}"


def build_organization_job_payload(
    snapshot: MemoryOrganizationSnapshot,
    organization_model: MemoryOrganizationModelConfig,
    trigger: str = "manual",
) -> dict[str, Any]:
    if not isinstance(trigger, str) or trigger not in _ORGANIZATION_TRIGGERS:
        _raise_organization_payload_invalid()
    payload = {
        "trigger": trigger,
        "snapshot": snapshot.to_job_snapshot(),
        "organization_model": organization_model.to_job_snapshot(),
    }
    canonical_json_dumps(payload)
    return payload


def build_organization_merge_child_payload(
    item: MemoryOrganizationValidatedItem,
    *,
    parent_job_id: int,
    group_index: int,
    snapshot_digest: str,
    active_embedding_revision: int,
    index_revision: int,
    policy_version: int,
) -> dict[str, Any]:
    if not isinstance(item, MemoryOrganizationValidatedItem):
        raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
    if item.action not in {"update", "merge"} or item.target is None:
        raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
    if isinstance(parent_job_id, bool) or not isinstance(parent_job_id, int) or parent_job_id < 1:
        raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
    if isinstance(group_index, bool) or not isinstance(group_index, int) or group_index < 0:
        raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

    if item.action == "update":
        if len(item.sources) != 1 or item.primary_memory_id is not None:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        primary_memory_id = item.sources[0].memory_id
    else:
        if item.primary_memory_id is None or item.primary_memory_id not in {source.memory_id for source in item.sources}:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        primary_memory_id = item.primary_memory_id

    payload = {
        "parent_job_id": parent_job_id,
        "snapshot_digest": snapshot_digest,
        "active_embedding_revision": active_embedding_revision,
        "index_revision": index_revision,
        "policy_version": policy_version,
        "action": item.action,
        "sources": [
            {
                "memory_id": source.memory_id,
                "expected_version": source.expected_version,
                "pinned": source.pinned,
            }
            for source in item.sources
        ],
        "primary_memory_id": primary_memory_id,
        "target": {
            "content": item.target.content,
            "memory_key": item.target.memory_key,
            "memory_type": item.target.memory_type.value,
            "content_token_count": item.target.content_token_count,
            "content_hash": item.target.content_hash,
        },
    }
    canonical_json_dumps(payload)
    return payload


def build_organization_merge_child_dedupe_key(
    *,
    parent_job_id: int,
    group_index: int,
    payload: Mapping[str, Any],
) -> str:
    if isinstance(parent_job_id, bool) or not isinstance(parent_job_id, int) or parent_job_id < 1:
        raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
    if isinstance(group_index, bool) or not isinstance(group_index, int) or group_index < 0:
        raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
    if not isinstance(payload, dict):
        raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
    canonical = canonical_json_dumps(
        {
            "group_index": group_index,
            "parent_job_id": parent_job_id,
            "payload": payload,
            "scope": "memory_organization_merge_child",
        }
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"memory_organization_merge:{digest}"


def validate_organization_submission_store(store: LongTermMemoryStore) -> None:
    required = (
        store.active_embedding_channel_id,
        store.active_embedding_model_id,
        store.active_embedding_dimensions,
        store.active_embedding_signature,
        store.active_collection_name,
    )
    if (
        isinstance(store.active_embedding_revision, bool)
        or not isinstance(store.active_embedding_revision, int)
        or store.active_embedding_revision < 1
        or any(value is None or (isinstance(value, str) and not value.strip()) for value in required)
        or isinstance(store.active_embedding_channel_id, bool)
        or not isinstance(store.active_embedding_channel_id, int)
        or store.active_embedding_channel_id < 1
        or isinstance(store.active_embedding_dimensions, bool)
        or not isinstance(store.active_embedding_dimensions, int)
        or store.active_embedding_dimensions < 1
    ):
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)

    try:
        index_status = LongTermMemoryIndexStatus(store.index_status)
        migration_status = LongTermMemoryMigrationStatus(store.migration_status) if store.migration_status is not None else None
    except (TypeError, ValueError) as exc:
        raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT) from exc
    if index_status != LongTermMemoryIndexStatus.READY or migration_status in {
        LongTermMemoryMigrationStatus.PREPARING,
        LongTermMemoryMigrationStatus.BUILDING,
        LongTermMemoryMigrationStatus.CATCHING_UP,
        LongTermMemoryMigrationStatus.VALIDATING,
        LongTermMemoryMigrationStatus.SWITCHING,
    }:
        raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
