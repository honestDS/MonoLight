from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MANAGED_KNOWLEDGE_BASE_NOT_FOUND,
    ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED,
    ERR_MANAGED_KNOWLEDGE_ENUM_INVALID,
    ERR_MANAGED_KNOWLEDGE_FIELD_LENGTH_EXCEEDED,
    ERR_MANAGED_KNOWLEDGE_FIELD_REQUIRED,
    ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID,
    ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND,
    ERR_MANAGED_KNOWLEDGE_LLM_MAINTENANCE_FORBIDDEN,
    ERR_MANAGED_KNOWLEDGE_VERSION_CONFLICT,
    MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS,
    MANAGED_KNOWLEDGE_KEY_MAX_CHARS,
)
from app.core.crud.knowledge_base import knowledge_base_crud
from app.core.crud.managed_knowledge import managed_knowledge_item_crud, managed_knowledge_revision_crud
from app.core.knowledge.errors import ManagedKnowledgeConflictError, ManagedKnowledgeContentTooLongError, ManagedKnowledgeNotFoundError, ManagedKnowledgeValidationError
from app.core.knowledge.migration import record_knowledge_base_migration_change
from app.core.knowledge.results import ManagedKnowledgeMutationResult, ManagedKnowledgeMutationStatus
from app.core.utils.time import get_local_time
from app.core.utils.tokenizer import estimate_tokens
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseType,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevisionOperation,
    ManagedKnowledgeSourceType,
)

_UID_MAX_CHARS = 50


def _normalize_uid(value: Any) -> str:
    if not isinstance(value, str):
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, field="uid")
    if not value.strip():
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_REQUIRED, field="uid")
    if len(value) > _UID_MAX_CHARS:
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_LENGTH_EXCEEDED, field="uid", maximum=_UID_MAX_CHARS)
    return value


def _positive_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, field=field)
    return value


def normalize_managed_knowledge_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, field="knowledge_key")
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).strip())
    if not normalized:
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_REQUIRED, field="knowledge_key")
    if len(normalized) > MANAGED_KNOWLEDGE_KEY_MAX_CHARS:
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_LENGTH_EXCEEDED, field="knowledge_key", maximum=MANAGED_KNOWLEDGE_KEY_MAX_CHARS)
    return normalized


def _normalize_content(value: Any) -> tuple[str, int, str]:
    if not isinstance(value, str):
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, field="content")
    if not value.strip():
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_REQUIRED, field="content")
    # Managed knowledge deliberately has no character-count ceiling. The complete original text is the relational source of truth.
    token_count = estimate_tokens(value)
    if token_count > MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS:
        raise ManagedKnowledgeContentTooLongError(token_count)
    return value, token_count, hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_enum(value: Any, enum_type: type[StrEnum], *, field: str) -> StrEnum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_ENUM_INVALID, field=field) from exc


def _normalize_source_reference(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, field="source_reference")
    try:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, field="source_reference") from exc
    return json.loads(serialized)


def _normalize_optional_job_id(value: Any) -> int | None:
    return None if value is None else _positive_integer(value, field="source_job_id")


def _normalize_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, field=field)
    return value


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, StrEnum) else value


def _time_value(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def build_managed_knowledge_snapshot(item: ManagedKnowledgeItem) -> dict[str, Any]:
    return {
        "knowledge_id": item.id,
        "knowledge_base_id": item.knowledge_base_id,
        "uid": item.uid,
        "knowledge_key": item.knowledge_key,
        "content": item.content,
        "content_token_count": item.content_token_count,
        "content_hash": item.content_hash,
        "version": item.version,
        "source_type": _enum_value(item.source_type),
        "source_reference": item.source_reference,
        "source_job_id": item.source_job_id,
        "created_by": _enum_value(item.created_by),
        "last_modified_by": _enum_value(item.last_modified_by),
        "llm_maintainable": item.llm_maintainable,
        "indexed_version": item.indexed_version,
        "vector_item_ids": list(item.vector_item_ids or []),
        "is_recallable": item.is_recallable,
        "pending_job_id": item.pending_job_id,
        "created_at": _time_value(item.created_at),
        "updated_at": _time_value(item.updated_at),
        "deleted_at": _time_value(item.deleted_at),
        "last_recalled_at": _time_value(item.last_recalled_at),
    }


async def _finish(db: AsyncSession, *, commit: bool) -> None:
    if not isinstance(commit, bool):
        raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, field="commit")
    if commit:
        await db.commit()
    else:
        await db.flush()


async def _load_managed_container(db: AsyncSession, *, uid: str, knowledge_base_id: int) -> KnowledgeBase:
    knowledge_base = await knowledge_base_crud.get(db, knowledge_base_id)
    if knowledge_base is None or knowledge_base.uid != uid:
        raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_BASE_NOT_FOUND)
    if knowledge_base.knowledge_base_type != KnowledgeBaseType.LLM_MANAGED:
        raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED)
    return knowledge_base


async def _lock_managed_container(db: AsyncSession, *, uid: str, knowledge_base_id: int) -> KnowledgeBase:
    knowledge_base = await knowledge_base_crud.lock_owned_by_id(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )
    if knowledge_base is None:
        raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_BASE_NOT_FOUND)
    if knowledge_base.knowledge_base_type != KnowledgeBaseType.LLM_MANAGED:
        raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_BASE_NOT_MANAGED)
    return knowledge_base


async def _duplicate_result(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    knowledge_key: str,
    content_hash: str,
    exclude_id: int | None = None,
    current_read: bool = False,
) -> ManagedKnowledgeMutationResult | None:
    by_key = await managed_knowledge_item_crud.get_by_key(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        knowledge_key=knowledge_key,
        current_read=current_read,
    )
    if by_key is not None and by_key.id != exclude_id:
        return ManagedKnowledgeMutationResult(ManagedKnowledgeMutationStatus.EXISTING_KEY, by_key)
    by_content = await managed_knowledge_item_crud.get_by_content_hash(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        content_hash=content_hash,
        current_read=current_read,
    )
    if by_content is not None and by_content.id != exclude_id:
        return ManagedKnowledgeMutationResult(ManagedKnowledgeMutationStatus.EXISTING_CONTENT, by_content)
    return None


class ManagedKnowledgeService:
    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_key: str,
        content: str,
        source_type: ManagedKnowledgeSourceType | str,
        actor: ManagedKnowledgeActorType | str,
        source_reference: dict[str, Any] | None = None,
        source_job_id: int | None = None,
        llm_maintainable: bool | None = None,
        commit: bool = True,
    ) -> ManagedKnowledgeMutationResult:
        try:
            normalized_uid = _normalize_uid(uid)
            normalized_kb_id = _positive_integer(knowledge_base_id, field="knowledge_base_id")
            normalized_key = normalize_managed_knowledge_key(knowledge_key)
            normalized_content, token_count, content_hash = _normalize_content(content)
            normalized_source = _normalize_enum(source_type, ManagedKnowledgeSourceType, field="source_type")
            normalized_actor = _normalize_enum(actor, ManagedKnowledgeActorType, field="actor")
            normalized_reference = _normalize_source_reference(source_reference)
            normalized_job_id = _normalize_optional_job_id(source_job_id)
            maintainable = normalized_actor != ManagedKnowledgeActorType.USER if llm_maintainable is None else _normalize_bool(llm_maintainable, field="llm_maintainable")

            await _load_managed_container(db, uid=normalized_uid, knowledge_base_id=normalized_kb_id)
            duplicate = await _duplicate_result(db, uid=normalized_uid, knowledge_base_id=normalized_kb_id, knowledge_key=normalized_key, content_hash=content_hash)
            if duplicate is not None:
                await _finish(db, commit=commit)
                return duplicate

            try:
                async with db.begin_nested():
                    item = await managed_knowledge_item_crud.create(
                        db,
                        uid=normalized_uid,
                        knowledge_base_id=normalized_kb_id,
                        knowledge_key=normalized_key,
                        content=normalized_content,
                        content_token_count=token_count,
                        content_hash=content_hash,
                        version=1,
                        source_type=normalized_source,
                        source_reference=normalized_reference,
                        source_job_id=normalized_job_id,
                        created_by=normalized_actor,
                        last_modified_by=normalized_actor,
                        llm_maintainable=maintainable,
                        indexed_version=0,
                        vector_item_ids=[],
                        is_recallable=False,
                        pending_job_id=None,
                        commit=False,
                    )
                    await managed_knowledge_revision_crud.create(
                        db,
                        knowledge_base_id=normalized_kb_id,
                        uid=normalized_uid,
                        knowledge_id=item.id,
                        version=1,
                        operation=ManagedKnowledgeRevisionOperation.CREATE,
                        before_snapshot=None,
                        after_snapshot=build_managed_knowledge_snapshot(item),
                        source_type=normalized_source,
                        source_reference=normalized_reference,
                        source_job_id=normalized_job_id,
                        modified_by=normalized_actor,
                        commit=False,
                    )
                    knowledge_base = await _lock_managed_container(
                        db,
                        uid=normalized_uid,
                        knowledge_base_id=normalized_kb_id,
                    )
                    await record_knowledge_base_migration_change(
                        db,
                        knowledge_base=knowledge_base,
                        source_type=KnowledgeBaseMigrationSourceType.MANAGED_KNOWLEDGE,
                        source_id=item.id,
                        source_version=item.version,
                        action=KnowledgeBaseMigrationDeltaAction.UPSERT,
                    )
            except IntegrityError:
                duplicate = await _duplicate_result(
                    db,
                    uid=normalized_uid,
                    knowledge_base_id=normalized_kb_id,
                    knowledge_key=normalized_key,
                    content_hash=content_hash,
                    current_read=True,
                )
                if duplicate is None:
                    raise
                await _finish(db, commit=commit)
                return duplicate

            await _finish(db, commit=commit)
            return ManagedKnowledgeMutationResult(ManagedKnowledgeMutationStatus.CREATED, item)
        except Exception:
            if commit and db.in_transaction():
                await db.rollback()
            raise

    async def update(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        knowledge_key: str,
        content: str,
        source_type: ManagedKnowledgeSourceType | str,
        actor: ManagedKnowledgeActorType | str,
        source_reference: dict[str, Any] | None = None,
        source_job_id: int | None = None,
        llm_maintainable: bool | None = None,
        commit: bool = True,
    ) -> ManagedKnowledgeMutationResult:
        try:
            normalized_uid = _normalize_uid(uid)
            normalized_kb_id = _positive_integer(knowledge_base_id, field="knowledge_base_id")
            normalized_id = _positive_integer(knowledge_id, field="knowledge_id")
            normalized_version = _positive_integer(expected_version, field="expected_version")
            normalized_key = normalize_managed_knowledge_key(knowledge_key)
            normalized_content, token_count, content_hash = _normalize_content(content)
            normalized_source = _normalize_enum(source_type, ManagedKnowledgeSourceType, field="source_type")
            normalized_actor = _normalize_enum(actor, ManagedKnowledgeActorType, field="actor")
            normalized_reference = _normalize_source_reference(source_reference)
            normalized_job_id = _normalize_optional_job_id(source_job_id)

            await _load_managed_container(db, uid=normalized_uid, knowledge_base_id=normalized_kb_id)
            item = await managed_knowledge_item_crud.get_by_id(db, uid=normalized_uid, knowledge_base_id=normalized_kb_id, knowledge_id=normalized_id)
            if item is None or item.deleted_at is not None:
                raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND)
            if item.version != normalized_version:
                raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_VERSION_CONFLICT)
            if normalized_actor == ManagedKnowledgeActorType.LLM and not item.llm_maintainable:
                raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_LLM_MAINTENANCE_FORBIDDEN)

            if llm_maintainable is None:
                maintainable = False if normalized_actor == ManagedKnowledgeActorType.USER else item.llm_maintainable
            else:
                maintainable = _normalize_bool(llm_maintainable, field="llm_maintainable")
                if normalized_actor == ManagedKnowledgeActorType.LLM and maintainable != item.llm_maintainable:
                    raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_LLM_MAINTENANCE_FORBIDDEN)

            duplicate = await _duplicate_result(db, uid=normalized_uid, knowledge_base_id=normalized_kb_id, knowledge_key=normalized_key, content_hash=content_hash, exclude_id=normalized_id)
            if duplicate is not None:
                await _finish(db, commit=commit)
                return duplicate

            if item.knowledge_key == normalized_key and item.content == normalized_content and item.source_type == normalized_source and item.source_reference == normalized_reference and item.last_modified_by == normalized_actor and item.llm_maintainable == maintainable:
                await _finish(db, commit=commit)
                return ManagedKnowledgeMutationResult(ManagedKnowledgeMutationStatus.UNCHANGED, item)

            before_snapshot = build_managed_knowledge_snapshot(item)
            try:
                async with db.begin_nested():
                    updated = await managed_knowledge_item_crud.update_if_version(
                        db,
                        uid=normalized_uid,
                        knowledge_base_id=normalized_kb_id,
                        knowledge_id=normalized_id,
                        expected_version=normalized_version,
                        knowledge_key=normalized_key,
                        content=normalized_content,
                        content_token_count=token_count,
                        content_hash=content_hash,
                        source_type=normalized_source,
                        source_reference=normalized_reference,
                        source_job_id=normalized_job_id,
                        last_modified_by=normalized_actor,
                        llm_maintainable=maintainable,
                        is_recallable=False,
                        commit=False,
                    )
                    if updated is None:
                        raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_VERSION_CONFLICT)
                    await managed_knowledge_revision_crud.create(
                        db,
                        knowledge_base_id=normalized_kb_id,
                        uid=normalized_uid,
                        knowledge_id=normalized_id,
                        version=updated.version,
                        operation=ManagedKnowledgeRevisionOperation.UPDATE,
                        before_snapshot=before_snapshot,
                        after_snapshot=build_managed_knowledge_snapshot(updated),
                        source_type=normalized_source,
                        source_reference=normalized_reference,
                        source_job_id=normalized_job_id,
                        modified_by=normalized_actor,
                        commit=False,
                    )
                    knowledge_base = await _lock_managed_container(
                        db,
                        uid=normalized_uid,
                        knowledge_base_id=normalized_kb_id,
                    )
                    await record_knowledge_base_migration_change(
                        db,
                        knowledge_base=knowledge_base,
                        source_type=KnowledgeBaseMigrationSourceType.MANAGED_KNOWLEDGE,
                        source_id=updated.id,
                        source_version=updated.version,
                        action=KnowledgeBaseMigrationDeltaAction.UPSERT,
                    )
            except IntegrityError:
                duplicate = await _duplicate_result(
                    db,
                    uid=normalized_uid,
                    knowledge_base_id=normalized_kb_id,
                    knowledge_key=normalized_key,
                    content_hash=content_hash,
                    exclude_id=normalized_id,
                    current_read=True,
                )
                if duplicate is None:
                    raise
                await _finish(db, commit=commit)
                return duplicate

            await _finish(db, commit=commit)
            return ManagedKnowledgeMutationResult(ManagedKnowledgeMutationStatus.UPDATED, updated)
        except Exception:
            if commit and db.in_transaction():
                await db.rollback()
            raise

    async def delete(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        source_type: ManagedKnowledgeSourceType | str,
        actor: ManagedKnowledgeActorType | str,
        source_reference: dict[str, Any] | None = None,
        source_job_id: int | None = None,
        commit: bool = True,
    ) -> ManagedKnowledgeMutationResult:
        try:
            normalized_uid = _normalize_uid(uid)
            normalized_kb_id = _positive_integer(knowledge_base_id, field="knowledge_base_id")
            normalized_id = _positive_integer(knowledge_id, field="knowledge_id")
            normalized_version = _positive_integer(expected_version, field="expected_version")
            normalized_source = _normalize_enum(source_type, ManagedKnowledgeSourceType, field="source_type")
            normalized_actor = _normalize_enum(actor, ManagedKnowledgeActorType, field="actor")
            normalized_reference = _normalize_source_reference(source_reference)
            normalized_job_id = _normalize_optional_job_id(source_job_id)

            await _load_managed_container(db, uid=normalized_uid, knowledge_base_id=normalized_kb_id)
            item = await managed_knowledge_item_crud.get_by_id(db, uid=normalized_uid, knowledge_base_id=normalized_kb_id, knowledge_id=normalized_id)
            if item is None:
                raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND)
            if item.version != normalized_version:
                raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_VERSION_CONFLICT)
            if item.deleted_at is not None:
                await _finish(db, commit=commit)
                return ManagedKnowledgeMutationResult(ManagedKnowledgeMutationStatus.UNCHANGED, item)
            if normalized_actor == ManagedKnowledgeActorType.LLM and not item.llm_maintainable:
                raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_LLM_MAINTENANCE_FORBIDDEN)

            before_snapshot = build_managed_knowledge_snapshot(item)
            async with db.begin_nested():
                deleted = await managed_knowledge_item_crud.tombstone_if_version(
                    db,
                    uid=normalized_uid,
                    knowledge_base_id=normalized_kb_id,
                    knowledge_id=normalized_id,
                    expected_version=normalized_version,
                    source_type=normalized_source,
                    source_reference=normalized_reference,
                    source_job_id=normalized_job_id,
                    last_modified_by=normalized_actor,
                    is_recallable=False,
                    deleted_at=get_local_time(),
                    commit=False,
                )
                if deleted is None:
                    raise ManagedKnowledgeConflictError(ERR_MANAGED_KNOWLEDGE_VERSION_CONFLICT)
                await managed_knowledge_revision_crud.create(
                    db,
                    knowledge_base_id=normalized_kb_id,
                    uid=normalized_uid,
                    knowledge_id=normalized_id,
                    version=deleted.version,
                    operation=ManagedKnowledgeRevisionOperation.DELETE,
                    before_snapshot=before_snapshot,
                    after_snapshot=build_managed_knowledge_snapshot(deleted),
                    source_type=normalized_source,
                    source_reference=normalized_reference,
                    source_job_id=normalized_job_id,
                    modified_by=normalized_actor,
                    commit=False,
                )
                knowledge_base = await _lock_managed_container(
                    db,
                    uid=normalized_uid,
                    knowledge_base_id=normalized_kb_id,
                )
                await record_knowledge_base_migration_change(
                    db,
                    knowledge_base=knowledge_base,
                    source_type=KnowledgeBaseMigrationSourceType.MANAGED_KNOWLEDGE,
                    source_id=deleted.id,
                    source_version=deleted.version,
                    action=KnowledgeBaseMigrationDeltaAction.DELETE,
                )

            await _finish(db, commit=commit)
            return ManagedKnowledgeMutationResult(ManagedKnowledgeMutationStatus.DELETED, deleted)
        except Exception:
            if commit and db.in_transaction():
                await db.rollback()
            raise

    async def list_history(self, db: AsyncSession, *, uid: str, knowledge_base_id: int, knowledge_id: int, skip: int = 0, limit: int = 100):
        normalized_uid = _normalize_uid(uid)
        normalized_kb_id = _positive_integer(knowledge_base_id, field="knowledge_base_id")
        normalized_id = _positive_integer(knowledge_id, field="knowledge_id")
        if isinstance(skip, bool) or not isinstance(skip, int) or skip < 0:
            raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, field="skip")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ManagedKnowledgeValidationError(ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, field="limit")
        await _load_managed_container(db, uid=normalized_uid, knowledge_base_id=normalized_kb_id)
        history = await managed_knowledge_revision_crud.list_by_knowledge_id(
            db,
            uid=normalized_uid,
            knowledge_base_id=normalized_kb_id,
            knowledge_id=normalized_id,
            skip=skip,
            limit=limit,
        )
        if not history:
            raise ManagedKnowledgeNotFoundError(ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND)
        return history


managed_knowledge_service = ManagedKnowledgeService()

__all__ = [
    "ManagedKnowledgeConflictError",
    "ManagedKnowledgeContentTooLongError",
    "ManagedKnowledgeMutationResult",
    "ManagedKnowledgeNotFoundError",
    "ManagedKnowledgeService",
    "ManagedKnowledgeValidationError",
    "build_managed_knowledge_snapshot",
    "managed_knowledge_service",
]
