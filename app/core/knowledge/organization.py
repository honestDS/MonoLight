from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from time import monotonic
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID,
    ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID,
    ERR_MANAGED_KNOWLEDGE_BASE_NOT_FOUND,
    ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED,
    ERR_MANAGED_KNOWLEDGE_ORGANIZATION_LOCKED,
    KNOWLEDGE_ORGANIZATION_JOB_LEASE_RENEW_INTERVAL_SECONDS,
    KNOWLEDGE_ORGANIZATION_JOB_LEASE_SECONDS,
    KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.job import is_organization_operation, knowledge_job_crud
from app.core.crud.knowledge.managed import managed_knowledge_item_crud, managed_knowledge_revision_crud
from app.core.crud.knowledge.organization import (
    knowledge_organization_snapshot_crud,
    knowledge_organization_snapshot_item_crud,
)
from app.core.i18n import t
from app.core.knowledge.errors import ManagedKnowledgeConflictError, ManagedKnowledgeNotFoundError
from app.models.knowledge_base import (
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationSnapshotItem,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
)
from app.providers.database.time import get_database_time


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def build_knowledge_organization_work_identity(
    *,
    snapshot_key: str,
    organization_job_id: int,
    execution_model: Mapping[str, Any],
) -> tuple[str, str]:
    if isinstance(organization_job_id, bool) or not isinstance(organization_job_id, int) or organization_job_id < 1:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID))
    model_identity = {
        "channel_id": execution_model.get("channel_id"),
        "model_id": execution_model.get("model_id"),
        "protocol": execution_model.get("protocol"),
    }
    model_key = hashlib.sha256(canonical_json_dumps(model_identity).encode("utf-8")).hexdigest()
    work_payload = {
        "organization_job_id": organization_job_id,
        "scope": "knowledge_organization",
        "snapshot_key": snapshot_key,
        "version": 4,
    }
    work_key = hashlib.sha256(canonical_json_dumps(work_payload).encode("utf-8")).hexdigest()
    return work_key, model_key


def _require_snapshot_mapping(revision: ManagedKnowledgeRevision) -> Mapping[str, Any]:
    value = revision.after_snapshot
    if not isinstance(value, Mapping):
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
    return value


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


async def _validate_organization_job_for_lock(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    organization_job_id: int,
) -> KnowledgeJob:
    job = await knowledge_job_crud.get_by_id(db, uid=uid, job_id=organization_job_id)
    now = await get_database_time(db)
    if job is None or job.knowledge_base_id != knowledge_base_id or not is_organization_operation(job.operation) or job.status != KnowledgeJobStatus.RUNNING or not job.locked_by or job.lock_until is None or job.lock_until < now:
        raise ManagedKnowledgeConflictError(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID)
    return job


def _build_snapshot_lease_heartbeat(
    db: AsyncSession,
    *,
    job: KnowledgeJob,
) -> Callable[[bool], Awaitable[None]]:
    if job.id is None or not job.locked_by:
        raise ManagedKnowledgeConflictError(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID)
    last_renewed_at = 0.0

    async def renew(force: bool = False) -> None:
        nonlocal last_renewed_at
        current = monotonic()
        if not force and current - last_renewed_at < KNOWLEDGE_ORGANIZATION_JOB_LEASE_RENEW_INTERVAL_SECONDS:
            return
        renewed = await knowledge_job_crud.renew_lease(
            db,
            uid=job.uid,
            job_id=job.id,
            owner=job.locked_by,
            lease_seconds=KNOWLEDGE_ORGANIZATION_JOB_LEASE_SECONDS,
            commit=False,
        )
        if not renewed:
            raise ManagedKnowledgeConflictError(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID)
        last_renewed_at = monotonic()

    return renew


def _revision_is_organization_candidate(revision: ManagedKnowledgeRevision, item: ManagedKnowledgeItem | None) -> bool:
    if item is None:
        return False
    snapshot = _require_snapshot_mapping(revision)
    version = _positive_int(snapshot.get("version"))
    knowledge_id = _positive_int(snapshot.get("knowledge_id"))
    return bool(
        knowledge_id is not None
        and version is not None
        and revision.knowledge_id == knowledge_id
        and revision.version == version
        and item.id == knowledge_id
        and item.version == version
        and item.deleted_at is None
        and item.llm_maintainable is True
        and item.is_recallable is True
        and item.pending_job_id is None
        and item.indexed_version == version
    )


def _build_snapshot_item_from_revision(revision: ManagedKnowledgeRevision, item: ManagedKnowledgeItem) -> dict[str, Any]:
    snapshot = _require_snapshot_mapping(revision)
    knowledge_id = _positive_int(snapshot.get("knowledge_id"))
    version = _positive_int(snapshot.get("version"))
    indexed_version = _positive_int(item.indexed_version)
    content_token_count = _non_negative_int(snapshot.get("content_token_count"))
    knowledge_key = snapshot.get("knowledge_key")
    content_hash = snapshot.get("content_hash")
    source_type = _enum_value(snapshot.get("source_type"))
    vector_item_ids = item.vector_item_ids
    if (
        knowledge_id is None
        or version is None
        or indexed_version is None
        or content_token_count is None
        or not isinstance(knowledge_key, str)
        or not knowledge_key
        or not isinstance(content_hash, str)
        or len(content_hash) != 64
        or not isinstance(source_type, str)
        or not source_type
        or not isinstance(vector_item_ids, list)
        or any(not isinstance(item_id, str) or not item_id for item_id in vector_item_ids)
        or revision.id is None
        or revision.knowledge_id != knowledge_id
        or revision.version != version
        or item.id != knowledge_id
        or item.version != version
        or item.knowledge_key != knowledge_key
        or item.content_hash != content_hash
        or item.content_token_count != content_token_count
    ):
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
    return {
        "knowledge_id": knowledge_id,
        "expected_version": version,
        "knowledge_key": knowledge_key,
        "content_hash": content_hash,
        "content_token_count": content_token_count,
        "content_reference": {
            "revision_id": revision.id,
            "version": version,
        },
        "source_type": source_type,
        "source_reference": snapshot.get("source_reference"),
        "llm_maintainable": True,
        "indexed_version": indexed_version,
        "vector_item_ids": list(vector_item_ids),
    }


async def _iter_candidate_revisions(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    boundary_revision_id: int,
    page_size: int,
) -> AsyncIterator[tuple[ManagedKnowledgeRevision, ManagedKnowledgeItem]]:
    after_knowledge_id = 0
    while True:
        page = await managed_knowledge_revision_crud.list_latest_at_boundary_page(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            boundary_revision_id=boundary_revision_id,
            after_knowledge_id=after_knowledge_id,
            limit=page_size,
        )
        if not page:
            return
        current_items = await managed_knowledge_item_crud.get_by_ids(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            knowledge_ids=[revision.knowledge_id for revision in page],
        )
        current_by_id = {item.id: item for item in current_items if item.id is not None}
        for revision in page:
            after_knowledge_id = max(after_knowledge_id, revision.knowledge_id)
            item = current_by_id.get(revision.knowledge_id)
            if _revision_is_organization_candidate(revision, item):
                yield revision, item
        if len(page) < page_size:
            return


def _snapshot_digest_prefix(
    *,
    uid: str,
    knowledge_base_id: int,
    boundary_revision_id: int,
    active_embedding_revision: int,
    index_revision: int,
) -> bytes:
    return canonical_json_dumps(
        {
            "active_embedding_revision": active_embedding_revision,
            "boundary_revision_id": boundary_revision_id,
            "index_revision": index_revision,
            "knowledge_base_id": knowledge_base_id,
            "scope": "knowledge_organization_snapshot_v2",
            "uid": uid,
        }
    ).encode("utf-8")


async def _calculate_snapshot_identity(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    boundary_revision_id: int,
    active_embedding_revision: int,
    index_revision: int,
    page_size: int,
    lease_heartbeat: Callable[[bool], Awaitable[None]] | None = None,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    digest.update(
        _snapshot_digest_prefix(
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            boundary_revision_id=boundary_revision_id,
            active_embedding_revision=active_embedding_revision,
            index_revision=index_revision,
        )
    )
    item_count = 0
    async for revision, item in _iter_candidate_revisions(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        boundary_revision_id=boundary_revision_id,
        page_size=page_size,
    ):
        if lease_heartbeat is not None:
            await lease_heartbeat(False)
        item_payload = _build_snapshot_item_from_revision(revision, item)
        encoded = canonical_json_dumps(item_payload).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
        digest.update(encoded)
        item_count += 1
    return digest.hexdigest(), item_count


async def _ensure_snapshot_items(
    db: AsyncSession,
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    page_size: int,
    organization_job_id: int | None = None,
    lease_heartbeat: Callable[[bool], Awaitable[None]] | None = None,
) -> None:
    if snapshot.id is None:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
    sequence = 0
    async for revision, item in _iter_candidate_revisions(
        db,
        uid=snapshot.uid,
        knowledge_base_id=snapshot.knowledge_base_id,
        boundary_revision_id=snapshot.boundary_revision_id,
        page_size=page_size,
    ):
        if lease_heartbeat is not None:
            await lease_heartbeat(False)
        item_payload = _build_snapshot_item_from_revision(revision, item)
        content_reference = item_payload["content_reference"]
        row = KnowledgeOrganizationSnapshotItem(
            snapshot_id=snapshot.id,
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
            sequence=sequence,
            knowledge_id=item_payload["knowledge_id"],
            expected_version=item_payload["expected_version"],
            knowledge_key=item_payload["knowledge_key"],
            content_hash=item_payload["content_hash"],
            content_token_count=item_payload["content_token_count"],
            revision_id=content_reference["revision_id"],
            source_type=item_payload["source_type"],
            source_reference=item_payload["source_reference"],
            llm_maintainable=True,
            indexed_version=item_payload["indexed_version"],
            vector_item_ids=item_payload["vector_item_ids"],
        )
        persisted, _ = await knowledge_organization_snapshot_item_crud.create_idempotent(
            db,
            item=row,
        )
        if persisted is None:
            await db.rollback()
            raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
        if organization_job_id is not None:
            locked = await managed_knowledge_item_crud.acquire_organization_lock(
                db,
                uid=snapshot.uid,
                knowledge_base_id=snapshot.knowledge_base_id,
                knowledge_id=item_payload["knowledge_id"],
                expected_version=item_payload["expected_version"],
                organization_job_id=organization_job_id,
            )
            if not locked:
                raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_ORGANIZATION_LOCKED)
        sequence += 1
    persisted_count = await knowledge_organization_snapshot_item_crud.count_for_snapshot(
        db,
        snapshot_id=snapshot.id,
    )
    if persisted_count != snapshot.item_count:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))


async def create_knowledge_organization_snapshot(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    organization_job_id: int | None = None,
) -> KnowledgeOrganizationSnapshot:
    knowledge_base = await knowledge_base_crud.lock_owned_by_id(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )
    if knowledge_base is None or knowledge_base.uid != uid:
        raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_BASE_NOT_FOUND)
    if knowledge_base.knowledge_base_type != KnowledgeBaseType.LLM_MANAGED:
        raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED)
    lease_heartbeat: Callable[[bool], Awaitable[None]] | None = None
    if organization_job_id is not None:
        organization_job = await _validate_organization_job_for_lock(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            organization_job_id=organization_job_id,
        )
        lease_heartbeat = _build_snapshot_lease_heartbeat(db, job=organization_job)
        await lease_heartbeat(True)

    boundary_revision_id = await managed_knowledge_revision_crud.get_boundary_revision_id(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )
    snapshot_key, item_count = await _calculate_snapshot_identity(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        boundary_revision_id=boundary_revision_id,
        active_embedding_revision=knowledge_base.active_embedding_revision,
        index_revision=knowledge_base.index_revision,
        page_size=KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE,
        lease_heartbeat=lease_heartbeat,
    )
    snapshot = KnowledgeOrganizationSnapshot(
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        snapshot_key=snapshot_key,
        boundary_revision_id=boundary_revision_id,
        active_embedding_revision=knowledge_base.active_embedding_revision,
        index_revision=knowledge_base.index_revision,
        item_count=item_count,
        items=[],
    )
    try:
        persisted, _ = await knowledge_organization_snapshot_crud.create_snapshot(db, snapshot=snapshot, commit=False)
        await _ensure_snapshot_items(
            db,
            snapshot=persisted,
            page_size=KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE,
            organization_job_id=organization_job_id,
            lease_heartbeat=lease_heartbeat,
        )
        await db.commit()
        return persisted
    except Exception:
        if db.in_transaction():
            await db.rollback()
        raise


def _revision_matches_snapshot_item(
    revision: ManagedKnowledgeRevision,
    snapshot_item: KnowledgeOrganizationSnapshotItem,
) -> bool:
    return revision.id == snapshot_item.revision_id and revision.uid == snapshot_item.uid and revision.knowledge_base_id == snapshot_item.knowledge_base_id and revision.knowledge_id == snapshot_item.knowledge_id and revision.version == snapshot_item.expected_version


def _resolve_snapshot_item(
    row: KnowledgeOrganizationSnapshotItem,
    revision: ManagedKnowledgeRevision,
) -> dict[str, Any]:
    if not _revision_matches_snapshot_item(revision, row):
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
    after_snapshot = _require_snapshot_mapping(revision)
    content = after_snapshot.get("content")
    if not isinstance(content, str) or not content or after_snapshot.get("knowledge_key") != row.knowledge_key or after_snapshot.get("content_hash") != row.content_hash or after_snapshot.get("content_token_count") != row.content_token_count or after_snapshot.get("llm_maintainable") is not True:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != row.content_hash:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
    return {
        "knowledge_id": row.knowledge_id,
        "expected_version": row.expected_version,
        "knowledge_key": row.knowledge_key,
        "content_hash": row.content_hash,
        "content_token_count": row.content_token_count,
        "content_reference": {
            "revision_id": row.revision_id,
            "version": row.expected_version,
        },
        "source_type": row.source_type,
        "source_reference": row.source_reference,
        "llm_maintainable": row.llm_maintainable,
        "indexed_version": row.indexed_version,
        "vector_item_ids": list(row.vector_item_ids or []),
        "content": content,
    }


async def load_knowledge_organization_snapshot_item_page(
    db: AsyncSession,
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    after_sequence: int = -1,
    limit: int = KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE,
) -> tuple[dict[str, Any], ...]:
    if snapshot.id is None or after_sequence < -1 or limit < 1:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
    page = await knowledge_organization_snapshot_item_crud.list_with_revision_page(
        db,
        snapshot_id=snapshot.id,
        after_sequence=after_sequence,
        limit=limit,
    )
    expected_sequence = after_sequence + 1
    resolved: list[dict[str, Any]] = []
    for row, revision in page:
        if row.sequence != expected_sequence:
            raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
        resolved.append(_resolve_snapshot_item(row, revision))
        expected_sequence += 1
    return tuple(resolved)


async def iter_knowledge_organization_snapshot_items(
    db: AsyncSession,
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    page_size: int = KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE,
) -> AsyncIterator[dict[str, Any]]:
    if snapshot.id is None or page_size < 1:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
    expected_sequence = 0
    while True:
        page = await load_knowledge_organization_snapshot_item_page(
            db,
            snapshot=snapshot,
            after_sequence=expected_sequence - 1,
            limit=page_size,
        )
        if not page:
            break
        for item in page:
            yield item
            expected_sequence += 1
        if len(page) < page_size:
            break
    if expected_sequence != snapshot.item_count:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))


async def load_knowledge_organization_snapshot_items(
    db: AsyncSession,
    *,
    snapshot: KnowledgeOrganizationSnapshot,
) -> tuple[dict[str, Any], ...]:
    items = [
        item
        async for item in iter_knowledge_organization_snapshot_items(
            db,
            snapshot=snapshot,
        )
    ]
    return tuple(items)


__all__ = [
    "build_knowledge_organization_work_identity",
    "create_knowledge_organization_snapshot",
    "iter_knowledge_organization_snapshot_items",
    "load_knowledge_organization_snapshot_item_page",
    "load_knowledge_organization_snapshot_items",
]
