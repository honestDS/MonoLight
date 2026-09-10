from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID,
    ERR_MANAGED_KNOWLEDGE_BASE_NOT_FOUND,
    ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.managed import managed_knowledge_item_crud, managed_knowledge_revision_crud
from app.core.crud.knowledge.organization import knowledge_organization_snapshot_crud
from app.core.i18n import t
from app.core.knowledge.errors import ManagedKnowledgeConflictError, ManagedKnowledgeNotFoundError
from app.models.knowledge_base import KnowledgeBaseType, KnowledgeOrganizationSnapshot, ManagedKnowledgeRevision


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def build_knowledge_organization_work_identity(
    *,
    snapshot_key: str,
    model_snapshot: Mapping[str, Any],
) -> tuple[str, str]:
    model_key = hashlib.sha256(canonical_json_dumps(dict(model_snapshot)).encode("utf-8")).hexdigest()
    work_payload = {
        "model_key": model_key,
        "scope": "knowledge_organization",
        "snapshot_key": snapshot_key,
    }
    work_key = hashlib.sha256(canonical_json_dumps(work_payload).encode("utf-8")).hexdigest()
    return work_key, model_key


def _build_snapshot_item(candidate) -> dict[str, Any]:
    item = candidate.item
    revision = candidate.revision
    return {
        "knowledge_id": item.id,
        "expected_version": item.version,
        "knowledge_key": item.knowledge_key,
        "content_hash": item.content_hash,
        "content_token_count": item.content_token_count,
        "content_reference": {
            "revision_id": revision.id,
            "version": revision.version,
        },
        "source_type": _enum_value(item.source_type),
        "source_reference": item.source_reference,
        "llm_maintainable": item.llm_maintainable,
        "indexed_version": item.indexed_version,
    }


def _build_snapshot_key(
    *,
    uid: str,
    knowledge_base_id: int,
    boundary_revision_id: int,
    active_embedding_revision: int,
    index_revision: int,
    items: list[dict[str, Any]],
) -> str:
    payload = {
        "active_embedding_revision": active_embedding_revision,
        "boundary_revision_id": boundary_revision_id,
        "index_revision": index_revision,
        "items": items,
        "knowledge_base_id": knowledge_base_id,
        "scope": "knowledge_organization_snapshot",
        "uid": uid,
    }
    return hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()


async def create_knowledge_organization_snapshot(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
) -> KnowledgeOrganizationSnapshot:
    knowledge_base = await knowledge_base_crud.get(db, knowledge_base_id)
    if knowledge_base is None or knowledge_base.uid != uid:
        raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_BASE_NOT_FOUND)
    if knowledge_base.knowledge_base_type != KnowledgeBaseType.LLM_MANAGED:
        raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED)

    candidates = await managed_knowledge_item_crud.list_organization_candidates(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )
    items = [_build_snapshot_item(candidate) for candidate in candidates]
    boundary_revision_id = max((candidate.revision.id or 0 for candidate in candidates), default=0)
    snapshot_key = _build_snapshot_key(
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        boundary_revision_id=boundary_revision_id,
        active_embedding_revision=knowledge_base.active_embedding_revision,
        index_revision=knowledge_base.index_revision,
        items=items,
    )
    snapshot = KnowledgeOrganizationSnapshot(
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        snapshot_key=snapshot_key,
        boundary_revision_id=boundary_revision_id,
        active_embedding_revision=knowledge_base.active_embedding_revision,
        index_revision=knowledge_base.index_revision,
        item_count=len(items),
        items=items,
    )
    persisted, _ = await knowledge_organization_snapshot_crud.create_snapshot(db, snapshot=snapshot)
    return persisted


def _revision_matches_snapshot_item(revision: ManagedKnowledgeRevision, snapshot_item: Mapping[str, Any]) -> bool:
    content_reference = snapshot_item.get("content_reference")
    if not isinstance(content_reference, Mapping):
        return False
    return revision.id == content_reference.get("revision_id") and revision.knowledge_id == snapshot_item.get("knowledge_id") and revision.version == snapshot_item.get("expected_version") and revision.version == content_reference.get("version")


async def load_knowledge_organization_snapshot_items(
    db: AsyncSession,
    *,
    snapshot: KnowledgeOrganizationSnapshot,
) -> tuple[dict[str, Any], ...]:
    revision_ids = []
    for item in snapshot.items:
        content_reference = item.get("content_reference") if isinstance(item, dict) else None
        revision_id = content_reference.get("revision_id") if isinstance(content_reference, dict) else None
        if not isinstance(revision_id, int) or isinstance(revision_id, bool) or revision_id < 1:
            raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
        revision_ids.append(revision_id)

    revisions = await managed_knowledge_revision_crud.get_by_ids(
        db,
        uid=snapshot.uid,
        knowledge_base_id=snapshot.knowledge_base_id,
        revision_ids=revision_ids,
    )
    revisions_by_id = {revision.id: revision for revision in revisions}
    resolved: list[dict[str, Any]] = []
    for item in snapshot.items:
        content_reference = item["content_reference"]
        revision = revisions_by_id.get(content_reference["revision_id"])
        if revision is None or not _revision_matches_snapshot_item(revision, item):
            raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
        after_snapshot = revision.after_snapshot
        content = after_snapshot.get("content") if isinstance(after_snapshot, dict) else None
        if not isinstance(content, str):
            raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))
        resolved.append({**item, "content": content})
    return tuple(resolved)


__all__ = [
    "build_knowledge_organization_work_identity",
    "create_knowledge_organization_snapshot",
    "load_knowledge_organization_snapshot_items",
]
