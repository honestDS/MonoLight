from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_PROFILE_NOT_FOUND, MSG_MANAGED_KNOWLEDGE_BASE_DEFAULT_NAME
from app.core.crud.knowledge_base import knowledge_base_crud, knowledge_base_profile_binding_crud
from app.core.crud.memory import memory_store_crud
from app.core.crud.profile import profile_crud
from app.core.embedding.common import load_embedding_runtime_config
from app.core.exceptions import BaseBusinessException, ResourceNotFoundException
from app.core.i18n import t
from app.core.knowledge.errors import (
    ManagedKnowledgeContainerConflictError,
    ManagedKnowledgeRuntimeUnavailableError,
)
from app.core.utils.database_integrity import is_unique_constraint_violation
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseIndexStatus, KnowledgeBaseType
from app.models.memory import LongTermMemoryStore

_MANAGED_PROFILE_CONSTRAINT = "uq_knowledge_base_managed_profile"
_PROFILE_BINDING_CONSTRAINT = "uq_knowledge_base_profile_binding_pair"


@dataclass(frozen=True, slots=True)
class ManagedKnowledgeContainerResult:
    knowledge_base: KnowledgeBase
    created: bool


@dataclass(frozen=True, slots=True)
class _MemoryEmbeddingSnapshot:
    channel_id: int
    model_id: str
    dimensions: int
    signature: str
    revision: int
    collection_name: str


def _is_managed_profile_unique_error(exc: IntegrityError) -> bool:
    return is_unique_constraint_violation(
        exc,
        constraint_names=(_MANAGED_PROFILE_CONSTRAINT,),
        fallback_marker_groups=(("knowledge_base.managed_profile_id",),),
    )


def _is_profile_binding_unique_error(exc: IntegrityError) -> bool:
    return is_unique_constraint_violation(
        exc,
        constraint_names=(_PROFILE_BINDING_CONSTRAINT,),
        fallback_marker_groups=(
            (
                "knowledge_base_profile_binding.knowledge_base_id",
                "knowledge_base_profile_binding.profile_id",
            ),
        ),
    )


def _memory_embedding_snapshot(store: LongTermMemoryStore | None) -> _MemoryEmbeddingSnapshot:
    if store is None:
        raise ManagedKnowledgeRuntimeUnavailableError()
    channel_id = store.active_embedding_channel_id
    model_id = store.active_embedding_model_id
    dimensions = store.active_embedding_dimensions
    signature = store.active_embedding_signature
    revision = store.active_embedding_revision
    collection_name = store.active_collection_name
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
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(collection_name, str)
        or not collection_name.strip()
    ):
        raise ManagedKnowledgeRuntimeUnavailableError()
    return _MemoryEmbeddingSnapshot(
        channel_id=channel_id,
        model_id=model_id.strip(),
        dimensions=dimensions,
        signature=signature.strip(),
        revision=revision,
        collection_name=collection_name.strip(),
    )


async def _lock_memory_embedding_channel(
    db: AsyncSession,
    *,
    uid: str,
) -> tuple[_MemoryEmbeddingSnapshot, int | None]:
    initial = _memory_embedding_snapshot(await memory_store_crud.get_snapshot_by_uid(db, uid=uid))
    try:
        runtime_config = await load_embedding_runtime_config(
            db,
            initial.channel_id,
            initial.model_id,
            lock_for_reference_write=True,
        )
    except (HTTPException, BaseBusinessException, TypeError, ValueError) as exc:
        raise ManagedKnowledgeRuntimeUnavailableError() from exc
    return initial, runtime_config.declared_dimensions


async def _lock_current_memory_embedding(
    db: AsyncSession,
    *,
    uid: str,
    expected: _MemoryEmbeddingSnapshot,
    declared_dimensions: int | None,
) -> _MemoryEmbeddingSnapshot:
    current = _memory_embedding_snapshot(await memory_store_crud.lock_for_mutation(db, uid=uid, commit=False))
    if current != expected:
        raise ManagedKnowledgeContainerConflictError()
    if declared_dimensions is not None and declared_dimensions != current.dimensions:
        raise ManagedKnowledgeRuntimeUnavailableError()
    return current


def _managed_collection_name() -> str:
    return f"managed_kb_{uuid.uuid4().hex}"


def _managed_display_name(profile_name: str) -> str:
    return t(MSG_MANAGED_KNOWLEDGE_BASE_DEFAULT_NAME, profile_name=profile_name)[:100]


async def _ensure_profile_binding(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    profile_id: int,
) -> None:
    if await knowledge_base_profile_binding_crud.get(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        profile_id=profile_id,
    ):
        return
    try:
        async with db.begin_nested():
            await knowledge_base_profile_binding_crud.create(
                db,
                uid=uid,
                knowledge_base_id=knowledge_base_id,
                profile_id=profile_id,
            )
    except IntegrityError as exc:
        if not _is_profile_binding_unique_error(exc):
            raise
        if (
            await knowledge_base_profile_binding_crud.lock(
                db,
                uid=uid,
                knowledge_base_id=knowledge_base_id,
                profile_id=profile_id,
            )
            is None
        ):
            raise ManagedKnowledgeContainerConflictError() from exc


async def get_or_create_managed_knowledge_base(
    db: AsyncSession,
    *,
    uid: str,
    profile_id: int,
) -> ManagedKnowledgeContainerResult:
    profile_snapshot = await profile_crud.get_snapshot(db, profile_id)
    if profile_snapshot is None or profile_snapshot.uid != uid or profile_snapshot.id is None:
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)

    existing = await knowledge_base_crud.get_managed_by_profile(
        db,
        uid=uid,
        profile_id=profile_id,
    )
    if existing is not None:
        profile = await profile_crud.lock_for_runtime_use(
            db,
            profile_id=profile_id,
            uid=uid,
        )
        if profile is None or profile.id is None:
            raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
        existing = await knowledge_base_crud.lock_managed_by_profile(
            db,
            uid=uid,
            profile_id=profile_id,
        )
        if existing is None:
            raise ManagedKnowledgeContainerConflictError()
        if existing.id is None:
            raise ManagedKnowledgeContainerConflictError()
        await _ensure_profile_binding(
            db,
            uid=uid,
            knowledge_base_id=existing.id,
            profile_id=profile_id,
        )
        return ManagedKnowledgeContainerResult(existing, False)

    expected_memory, declared_dimensions = await _lock_memory_embedding_channel(
        db,
        uid=uid,
    )
    profile = await profile_crud.lock_for_runtime_use(
        db,
        profile_id=profile_id,
        uid=uid,
    )
    if profile is None or profile.id is None:
        raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
    memory = await _lock_current_memory_embedding(
        db,
        uid=uid,
        expected=expected_memory,
        declared_dimensions=declared_dimensions,
    )

    existing = await knowledge_base_crud.lock_managed_by_profile(
        db,
        uid=uid,
        profile_id=profile_id,
    )
    if existing is not None:
        if existing.id is None:
            raise ManagedKnowledgeContainerConflictError()
        await _ensure_profile_binding(
            db,
            uid=uid,
            knowledge_base_id=existing.id,
            profile_id=profile_id,
        )
        return ManagedKnowledgeContainerResult(existing, False)

    collection_name = _managed_collection_name()
    try:
        async with db.begin_nested():
            knowledge_base = await knowledge_base_crud.create(
                db,
                obj_in={
                    "uid": uid,
                    "name": _managed_display_name(profile.name),
                    "description": None,
                    "embedding_channel_id": memory.channel_id,
                    "embedding_model_id": memory.model_id,
                    "embedding_dimensions": memory.dimensions,
                    "collection_name": collection_name,
                    "knowledge_base_type": KnowledgeBaseType.LLM_MANAGED,
                    "managed_profile_id": profile_id,
                    "active_embedding_channel_id": memory.channel_id,
                    "active_embedding_model_id": memory.model_id,
                    "active_embedding_dimensions": memory.dimensions,
                    "active_embedding_signature": memory.signature,
                    "active_embedding_revision": memory.revision,
                    "active_collection_name": collection_name,
                    "index_revision": 1,
                    "index_status": KnowledgeBaseIndexStatus.PENDING,
                },
                commit=False,
            )
    except IntegrityError as exc:
        if not _is_managed_profile_unique_error(exc):
            raise
        knowledge_base = await knowledge_base_crud.lock_managed_by_profile(
            db,
            uid=uid,
            profile_id=profile_id,
        )
        if knowledge_base is None:
            raise ManagedKnowledgeContainerConflictError() from exc
        created = False
    else:
        created = True

    if knowledge_base.id is None:
        raise ManagedKnowledgeContainerConflictError()
    await _ensure_profile_binding(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base.id,
        profile_id=profile_id,
    )
    return ManagedKnowledgeContainerResult(knowledge_base, created)


__all__ = [
    "ManagedKnowledgeContainerResult",
    "get_or_create_managed_knowledge_base",
]
