from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID,
    ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID,
)
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.knowledge_base import (
    KnowledgeBase,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeRevisionOperation,
    ManagedKnowledgeSourceType,
)


@dataclass(frozen=True, slots=True)
class ManagedKnowledgeRecallState:
    id: int
    version: int
    indexed_version: int
    vector_item_ids: tuple[str, ...]
    is_recallable: bool
    deleted_at: datetime | None


@dataclass(frozen=True, slots=True)
class ManagedKnowledgeOrganizationCandidate:
    item: ManagedKnowledgeItem
    revision: ManagedKnowledgeRevision


async def _finish(db: AsyncSession, *, commit: bool) -> None:
    if commit:
        await db.commit()
    else:
        await db.flush()


def organization_lock_token_for_job(job_id: int) -> str:
    if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
        raise ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_RUN_INVALID))
    return f"job:{job_id}"


class CRUDManagedKnowledgeItem:
    async def list_organization_candidates(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
    ) -> list[ManagedKnowledgeOrganizationCandidate]:
        result = await db.execute(
            select(ManagedKnowledgeItem, ManagedKnowledgeRevision)
            .join(
                ManagedKnowledgeRevision,
                (ManagedKnowledgeRevision.uid == ManagedKnowledgeItem.uid) & (ManagedKnowledgeRevision.knowledge_base_id == ManagedKnowledgeItem.knowledge_base_id) & (ManagedKnowledgeRevision.knowledge_id == ManagedKnowledgeItem.id) & (ManagedKnowledgeRevision.version == ManagedKnowledgeItem.version),
            )
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.deleted_at.is_(None),
                ManagedKnowledgeItem.llm_maintainable.is_(True),
                ManagedKnowledgeItem.is_recallable.is_(True),
                ManagedKnowledgeItem.pending_job_id.is_(None),
                ManagedKnowledgeItem.indexed_version == ManagedKnowledgeItem.version,
            )
            .order_by(ManagedKnowledgeItem.id)
        )
        return [ManagedKnowledgeOrganizationCandidate(item=item, revision=revision) for item, revision in result.all()]

    async def count_organization_candidates(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        updated_after: datetime | None = None,
    ) -> int:
        filters = [
            ManagedKnowledgeItem.uid == uid,
            ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
            ManagedKnowledgeItem.deleted_at.is_(None),
            ManagedKnowledgeItem.llm_maintainable.is_(True),
            ManagedKnowledgeItem.is_recallable.is_(True),
            ManagedKnowledgeItem.pending_job_id.is_(None),
            ManagedKnowledgeItem.indexed_version == ManagedKnowledgeItem.version,
        ]
        if updated_after is not None:
            filters.append(ManagedKnowledgeItem.updated_at > updated_after)
        result = await db.execute(
            select(func.count())
            .select_from(ManagedKnowledgeItem)
            .join(
                ManagedKnowledgeRevision,
                (ManagedKnowledgeRevision.uid == ManagedKnowledgeItem.uid) & (ManagedKnowledgeRevision.knowledge_base_id == ManagedKnowledgeItem.knowledge_base_id) & (ManagedKnowledgeRevision.knowledge_id == ManagedKnowledgeItem.id) & (ManagedKnowledgeRevision.version == ManagedKnowledgeItem.version),
            )
            .where(*filters)
        )
        return int(result.scalar() or 0)

    async def list_page(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        skip: int = 0,
        limit: int = 20,
        query: str | None = None,
    ) -> tuple[list[ManagedKnowledgeItem], int]:
        filters = [
            ManagedKnowledgeItem.uid == uid,
            ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
            ManagedKnowledgeItem.deleted_at.is_(None),
        ]
        normalized_query = (query or "").strip()
        if normalized_query:
            pattern = f"%{normalized_query}%"
            filters.append(
                or_(
                    ManagedKnowledgeItem.knowledge_key.ilike(pattern),
                    ManagedKnowledgeItem.content.ilike(pattern),
                )
            )
        total_result = await db.execute(select(func.count()).select_from(ManagedKnowledgeItem).where(*filters))
        items_result = await db.execute(select(ManagedKnowledgeItem).where(*filters).order_by(ManagedKnowledgeItem.updated_at.desc(), ManagedKnowledgeItem.id.desc()).offset(skip).limit(limit))
        return list(items_result.scalars().all()), int(total_result.scalar() or 0)

    async def _lock_knowledge_base_for_write(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
    ) -> None:
        if db.get_bind().dialect.name == "sqlite":
            return
        await db.execute(
            select(KnowledgeBase.id)
            .where(
                KnowledgeBase.uid == uid,
                KnowledgeBase.id == knowledge_base_id,
            )
            .with_for_update()
        )

    async def count_by_knowledge_base(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
    ) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(ManagedKnowledgeItem)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
            )
        )
        return int(result.scalar() or 0)

    async def count_organization_locked(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
    ) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(ManagedKnowledgeItem)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.organization_lock_token.is_not(None),
            )
        )
        return int(result.scalar() or 0)

    async def get_by_id(self, db: AsyncSession, *, uid: str, knowledge_base_id: int, knowledge_id: int) -> ManagedKnowledgeItem | None:
        result = await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.uid == uid, ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id, ManagedKnowledgeItem.id == knowledge_id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_by_key(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_key: str,
        current_read: bool = False,
    ) -> ManagedKnowledgeItem | None:
        statement = select(ManagedKnowledgeItem).where(
            ManagedKnowledgeItem.uid == uid,
            ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
            ManagedKnowledgeItem.knowledge_key == knowledge_key,
        )
        if current_read:
            # MySQL InnoDB locking reads bypass the REPEATABLE READ snapshot and see the committed conflict winner; SQLite safely ignores FOR UPDATE.
            statement = statement.with_for_update()
        result = await db.execute(statement.execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_by_content_hash(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        content_hash: str,
        current_read: bool = False,
    ) -> ManagedKnowledgeItem | None:
        statement = select(ManagedKnowledgeItem).where(
            ManagedKnowledgeItem.uid == uid,
            ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
            ManagedKnowledgeItem.content_hash == content_hash,
        )
        if current_read:
            # MySQL InnoDB locking reads bypass the REPEATABLE READ snapshot and see the committed conflict winner; SQLite safely ignores FOR UPDATE.
            statement = statement.with_for_update()
        result = await db.execute(statement.execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_by_ids(self, db: AsyncSession, *, uid: str, knowledge_base_id: int, knowledge_ids: Iterable[int]) -> list[ManagedKnowledgeItem]:
        ids = tuple(dict.fromkeys(knowledge_ids))
        if not ids:
            return []
        result = await db.execute(
            select(ManagedKnowledgeItem)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.id.in_(ids),
            )
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def get_recall_states_by_ids(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_ids: Iterable[int],
    ) -> list[ManagedKnowledgeRecallState]:
        ids = tuple(dict.fromkeys(knowledge_ids))
        if not ids:
            return []
        result = await db.execute(
            select(
                ManagedKnowledgeItem.id,
                ManagedKnowledgeItem.version,
                ManagedKnowledgeItem.indexed_version,
                ManagedKnowledgeItem.vector_item_ids,
                ManagedKnowledgeItem.is_recallable,
                ManagedKnowledgeItem.deleted_at,
            ).where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.id.in_(ids),
            )
        )
        return [
            ManagedKnowledgeRecallState(
                id=row.id,
                version=row.version,
                indexed_version=row.indexed_version,
                vector_item_ids=tuple(row.vector_item_ids or ()),
                is_recallable=bool(row.is_recallable),
                deleted_at=row.deleted_at,
            )
            for row in result.all()
        ]

    async def create(self, db: AsyncSession, *, commit: bool = True, **values: Any) -> ManagedKnowledgeItem:
        uid = values.get("uid")
        knowledge_base_id = values.get("knowledge_base_id")
        if isinstance(uid, str) and isinstance(knowledge_base_id, int):
            await self._lock_knowledge_base_for_write(
                db,
                uid=uid,
                knowledge_base_id=knowledge_base_id,
            )
        item = ManagedKnowledgeItem.model_validate(values)
        db.add(item)
        await _finish(db, commit=commit)
        await db.refresh(item)
        return item

    async def update_if_version(self, db: AsyncSession, *, uid: str, knowledge_base_id: int, knowledge_id: int, expected_version: int, organization_lock_token: str | None = None, commit: bool = True, **values: Any) -> ManagedKnowledgeItem | None:
        await self._lock_knowledge_base_for_write(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        organization_lock_condition = ManagedKnowledgeItem.organization_lock_token.is_(None) if organization_lock_token is None else ManagedKnowledgeItem.organization_lock_token == organization_lock_token
        update_values = dict(values)
        for protected in ("id", "uid", "knowledge_base_id", "version", "created_at"):
            update_values.pop(protected, None)
        update_values["version"] = ManagedKnowledgeItem.version + 1
        update_values["updated_at"] = get_local_time()
        result = await db.execute(
            update(ManagedKnowledgeItem)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.id == knowledge_id,
                ManagedKnowledgeItem.version == expected_version,
                ManagedKnowledgeItem.deleted_at.is_(None),
                organization_lock_condition,
            )
            .values(**update_values)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        return await self.get_by_id(db, uid=uid, knowledge_base_id=knowledge_base_id, knowledge_id=knowledge_id)

    async def tombstone_if_version(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        organization_lock_token: str | None = None,
        commit: bool = True,
        **values: Any,
    ) -> ManagedKnowledgeItem | None:
        return await self.update_if_version(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            knowledge_id=knowledge_id,
            expected_version=expected_version,
            organization_lock_token=organization_lock_token,
            commit=commit,
            **values,
        )

    async def bind_pending_job(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        job_id: int,
        source_job_id: int | None = None,
        organization_lock_token: str | None = None,
        commit: bool = True,
    ) -> ManagedKnowledgeItem | None:
        await self._lock_knowledge_base_for_write(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        values: dict[str, Any] = {
            "pending_job_id": job_id,
            "updated_at": get_local_time(),
        }
        if source_job_id is not None:
            values["source_job_id"] = source_job_id
        organization_lock_condition = ManagedKnowledgeItem.organization_lock_token.is_(None) if organization_lock_token is None else ManagedKnowledgeItem.organization_lock_token == organization_lock_token
        result = await db.execute(
            update(ManagedKnowledgeItem)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.id == knowledge_id,
                ManagedKnowledgeItem.version == expected_version,
                ManagedKnowledgeItem.pending_job_id.is_(None),
                organization_lock_condition,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        return await self.get_by_id(db, uid=uid, knowledge_base_id=knowledge_base_id, knowledge_id=knowledge_id)

    async def clear_organization_merge_unique_fields(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        pending_job_id: int,
        organization_lock_token: str,
        commit: bool = True,
    ) -> bool:
        await self._lock_knowledge_base_for_write(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        result = await db.execute(
            update(ManagedKnowledgeItem)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.id == knowledge_id,
                ManagedKnowledgeItem.version == expected_version,
                ManagedKnowledgeItem.pending_job_id == pending_job_id,
                ManagedKnowledgeItem.deleted_at.is_not(None),
                ManagedKnowledgeItem.is_recallable.is_(False),
                ManagedKnowledgeItem.organization_lock_token == organization_lock_token,
            )
            .values(
                knowledge_key=None,
                content_hash=None,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def publish_indexed_version(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        job_id: int,
        vector_item_ids: list[str],
        commit: bool = True,
    ) -> ManagedKnowledgeItem | None:
        await self._lock_knowledge_base_for_write(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        result = await db.execute(
            update(ManagedKnowledgeItem)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.id == knowledge_id,
                ManagedKnowledgeItem.version == expected_version,
                ManagedKnowledgeItem.pending_job_id == job_id,
                ManagedKnowledgeItem.deleted_at.is_(None),
            )
            .values(
                indexed_version=expected_version,
                vector_item_ids=vector_item_ids,
                is_recallable=True,
                pending_job_id=None,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        return await self.get_by_id(db, uid=uid, knowledge_base_id=knowledge_base_id, knowledge_id=knowledge_id)

    async def hard_delete_tombstoned(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        job_id: int,
        commit: bool = True,
    ) -> bool:
        await self._lock_knowledge_base_for_write(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        result = await db.execute(
            delete(ManagedKnowledgeItem).where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.id == knowledge_id,
                ManagedKnowledgeItem.version == expected_version,
                ManagedKnowledgeItem.pending_job_id == job_id,
                ManagedKnowledgeItem.deleted_at.is_not(None),
                ManagedKnowledgeItem.is_recallable.is_(False),
            )
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def rollback_pending_organization_mutation(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        job_id: int,
        updated_at: datetime | None = None,
        commit: bool = True,
    ) -> bool:
        await self._lock_knowledge_base_for_write(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        item_result = await db.execute(
            select(ManagedKnowledgeItem.id)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.id == knowledge_id,
                ManagedKnowledgeItem.version == expected_version,
                or_(
                    ManagedKnowledgeItem.pending_job_id == job_id,
                    ManagedKnowledgeItem.pending_job_id.is_(None),
                ),
            )
            .with_for_update()
        )
        if item_result.scalar_one_or_none() is None:
            await _finish(db, commit=commit)
            return False

        revision_result = await db.execute(
            select(ManagedKnowledgeRevision)
            .where(
                ManagedKnowledgeRevision.uid == uid,
                ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeRevision.knowledge_id == knowledge_id,
                ManagedKnowledgeRevision.version == expected_version,
                ManagedKnowledgeRevision.source_job_id == job_id,
                ManagedKnowledgeRevision.operation.in_(
                    (
                        ManagedKnowledgeRevisionOperation.UPDATE,
                        ManagedKnowledgeRevisionOperation.DELETE,
                    )
                ),
            )
            .with_for_update()
        )
        revision = revision_result.scalar_one_or_none()

        def _snapshot_invalid() -> ValueError:
            return ValueError(t(ERR_KNOWLEDGE_ORGANIZATION_SNAPSHOT_INVALID))

        required_snapshot_fields = {
            "knowledge_id",
            "knowledge_base_id",
            "uid",
            "knowledge_key",
            "content",
            "content_token_count",
            "content_hash",
            "version",
            "source_type",
            "source_reference",
            "source_job_id",
            "created_by",
            "last_modified_by",
            "llm_maintainable",
            "indexed_version",
            "vector_item_ids",
            "is_recallable",
            "pending_job_id",
            "created_at",
            "updated_at",
            "deleted_at",
            "last_recalled_at",
        }
        if revision is None or revision.id is None or not isinstance(revision.before_snapshot, Mapping) or not required_snapshot_fields.issubset(revision.before_snapshot):
            raise _snapshot_invalid()

        snapshot = revision.before_snapshot
        snapshot_uid = snapshot["uid"]
        snapshot_knowledge_base_id = snapshot["knowledge_base_id"]
        snapshot_knowledge_id = snapshot["knowledge_id"]
        snapshot_version = snapshot["version"]
        snapshot_key = snapshot["knowledge_key"]
        snapshot_content = snapshot["content"]
        snapshot_content_token_count = snapshot["content_token_count"]
        snapshot_content_hash = snapshot["content_hash"]
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 2
            or not isinstance(snapshot_uid, str)
            or snapshot_uid != uid
            or isinstance(snapshot_knowledge_base_id, bool)
            or not isinstance(snapshot_knowledge_base_id, int)
            or snapshot_knowledge_base_id != knowledge_base_id
            or isinstance(snapshot_knowledge_id, bool)
            or not isinstance(snapshot_knowledge_id, int)
            or snapshot_knowledge_id != knowledge_id
            or isinstance(snapshot_version, bool)
            or not isinstance(snapshot_version, int)
            or snapshot_version < 1
            or snapshot_version != expected_version - 1
            or not isinstance(snapshot_key, str)
            or not snapshot_key.strip()
            or len(snapshot_key) > 255
            or not isinstance(snapshot_content, str)
            or not snapshot_content.strip()
            or isinstance(snapshot_content_token_count, bool)
            or not isinstance(snapshot_content_token_count, int)
            or snapshot_content_token_count < 0
            or not isinstance(snapshot_content_hash, str)
            or len(snapshot_content_hash) != 64
            or any(character not in "0123456789abcdef" for character in snapshot_content_hash)
        ):
            raise _snapshot_invalid()
        if snapshot_content_hash != hashlib.sha256(snapshot_content.encode("utf-8")).hexdigest():
            raise _snapshot_invalid()

        try:
            snapshot_source_type = ManagedKnowledgeSourceType(snapshot["source_type"])
            snapshot_created_by = ManagedKnowledgeActorType(snapshot["created_by"])
            snapshot_last_modified_by = ManagedKnowledgeActorType(snapshot["last_modified_by"])
        except (TypeError, ValueError) as exc:
            raise _snapshot_invalid() from exc

        snapshot_source_reference = snapshot["source_reference"]
        if snapshot_source_reference is not None and not isinstance(snapshot_source_reference, dict):
            raise _snapshot_invalid()
        snapshot_source_job_id = snapshot["source_job_id"]
        if snapshot_source_job_id is not None and (isinstance(snapshot_source_job_id, bool) or not isinstance(snapshot_source_job_id, int) or snapshot_source_job_id < 1):
            raise _snapshot_invalid()

        snapshot_llm_maintainable = snapshot["llm_maintainable"]
        snapshot_indexed_version = snapshot["indexed_version"]
        snapshot_vector_item_ids = snapshot["vector_item_ids"]
        snapshot_is_recallable = snapshot["is_recallable"]
        if (
            not isinstance(snapshot_llm_maintainable, bool)
            or isinstance(snapshot_indexed_version, bool)
            or not isinstance(snapshot_indexed_version, int)
            or snapshot_indexed_version != snapshot_version
            or not isinstance(snapshot_vector_item_ids, list)
            or any(not isinstance(item_id, str) or not item_id for item_id in snapshot_vector_item_ids)
            or snapshot_is_recallable is not True
            or snapshot["pending_job_id"] is not None
        ):
            raise _snapshot_invalid()

        def _snapshot_time(field: str, *, nullable: bool) -> datetime | None:
            value = snapshot[field]
            if value is None:
                if nullable:
                    return None
                raise _snapshot_invalid()
            if isinstance(value, datetime):
                return value
            if not isinstance(value, str):
                raise _snapshot_invalid()
            try:
                return datetime.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise _snapshot_invalid() from exc

        snapshot_created_at = _snapshot_time("created_at", nullable=False)
        _snapshot_time("updated_at", nullable=False)
        snapshot_deleted_at = _snapshot_time("deleted_at", nullable=True)
        snapshot_last_recalled_at = _snapshot_time("last_recalled_at", nullable=True)
        if snapshot_deleted_at is not None:
            raise _snapshot_invalid()

        restored_values = {
            "knowledge_key": snapshot_key,
            "content": snapshot_content,
            "content_token_count": snapshot_content_token_count,
            "content_hash": snapshot_content_hash,
            "version": snapshot_version,
            "source_type": snapshot_source_type,
            "source_reference": None if snapshot_source_reference is None else dict(snapshot_source_reference),
            "source_job_id": snapshot_source_job_id,
            "created_by": snapshot_created_by,
            "last_modified_by": snapshot_last_modified_by,
            "llm_maintainable": snapshot_llm_maintainable,
            "indexed_version": snapshot_indexed_version,
            "vector_item_ids": list(snapshot_vector_item_ids),
            "is_recallable": snapshot_is_recallable,
            "pending_job_id": None,
            "created_at": snapshot_created_at,
            "updated_at": updated_at if updated_at is not None else get_local_time(),
            "deleted_at": snapshot_deleted_at,
            "last_recalled_at": snapshot_last_recalled_at,
        }

        transaction = await db.begin_nested()
        try:
            deleted_revision = await db.execute(
                delete(ManagedKnowledgeRevision).where(
                    ManagedKnowledgeRevision.id == revision.id,
                    ManagedKnowledgeRevision.uid == uid,
                    ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id,
                    ManagedKnowledgeRevision.knowledge_id == knowledge_id,
                    ManagedKnowledgeRevision.version == expected_version,
                    ManagedKnowledgeRevision.source_job_id == job_id,
                    ManagedKnowledgeRevision.operation.in_(
                        (
                            ManagedKnowledgeRevisionOperation.UPDATE,
                            ManagedKnowledgeRevisionOperation.DELETE,
                        )
                    ),
                )
            )
            if (deleted_revision.rowcount or 0) != 1:
                raise _snapshot_invalid()

            restored_item = await db.execute(
                update(ManagedKnowledgeItem)
                .where(
                    ManagedKnowledgeItem.uid == uid,
                    ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                    ManagedKnowledgeItem.id == knowledge_id,
                    ManagedKnowledgeItem.version == expected_version,
                    or_(
                        ManagedKnowledgeItem.pending_job_id == job_id,
                        ManagedKnowledgeItem.pending_job_id.is_(None),
                    ),
                )
                .values(**restored_values)
                .execution_options(synchronize_session=False)
            )
            if (restored_item.rowcount or 0) != 1:
                await transaction.rollback()
                return False
            await transaction.commit()
        except Exception:
            if transaction.is_active:
                await transaction.rollback()
            raise

        await _finish(db, commit=commit)
        return True

    async def acquire_organization_lock(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        expected_version: int,
        organization_job_id: int,
    ) -> bool:
        lock_token = organization_lock_token_for_job(organization_job_id)
        result = await db.execute(
            update(ManagedKnowledgeItem)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeItem.id == knowledge_id,
                ManagedKnowledgeItem.version == expected_version,
                ManagedKnowledgeItem.deleted_at.is_(None),
                ManagedKnowledgeItem.llm_maintainable.is_(True),
                ManagedKnowledgeItem.is_recallable.is_(True),
                ManagedKnowledgeItem.pending_job_id.is_(None),
                ManagedKnowledgeItem.indexed_version == expected_version,
                ManagedKnowledgeItem.organization_lock_token.is_(None),
            )
            .values(organization_lock_token=lock_token)
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def clear_organization_locks(
        self,
        db: AsyncSession,
        *,
        uid: str,
        organization_lock_token: str,
        updated_at: datetime | None = None,
        commit: bool = True,
    ) -> int:
        if not isinstance(organization_lock_token, str) or not organization_lock_token:
            return 0
        result = await db.execute(
            update(ManagedKnowledgeItem)
            .where(
                ManagedKnowledgeItem.uid == uid,
                ManagedKnowledgeItem.organization_lock_token == organization_lock_token,
            )
            .values(
                organization_lock_token=None,
                updated_at=updated_at if updated_at is not None else get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        rowcount = int(result.rowcount or 0)
        await _finish(db, commit=commit)
        return rowcount


class CRUDManagedKnowledgeRevision:
    async def get_by_version(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        knowledge_id: int,
        version: int,
    ) -> ManagedKnowledgeRevision | None:
        result = await db.execute(
            select(ManagedKnowledgeRevision).where(
                ManagedKnowledgeRevision.uid == uid,
                ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeRevision.knowledge_id == knowledge_id,
                ManagedKnowledgeRevision.version == version,
            )
        )
        return result.scalar_one_or_none()

    async def get_boundary_revision_id(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
    ) -> int:
        result = await db.execute(
            select(func.max(ManagedKnowledgeRevision.id)).where(
                ManagedKnowledgeRevision.uid == uid,
                ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id,
            )
        )
        return int(result.scalar() or 0)

    async def list_latest_at_boundary_page(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        boundary_revision_id: int,
        after_knowledge_id: int = 0,
        limit: int = 200,
    ) -> list[ManagedKnowledgeRevision]:
        latest = (
            select(
                ManagedKnowledgeRevision.knowledge_id.label("knowledge_id"),
                func.max(ManagedKnowledgeRevision.id).label("revision_id"),
            )
            .where(
                ManagedKnowledgeRevision.uid == uid,
                ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeRevision.id <= boundary_revision_id,
            )
            .group_by(ManagedKnowledgeRevision.knowledge_id)
            .subquery()
        )
        result = await db.execute(select(ManagedKnowledgeRevision).join(latest, ManagedKnowledgeRevision.id == latest.c.revision_id).where(ManagedKnowledgeRevision.knowledge_id > after_knowledge_id).order_by(ManagedKnowledgeRevision.knowledge_id).limit(limit))
        return list(result.scalars().all())

    async def get_by_ids(
        self,
        db: AsyncSession,
        *,
        uid: str,
        knowledge_base_id: int,
        revision_ids: Iterable[int],
    ) -> list[ManagedKnowledgeRevision]:
        ids = tuple(dict.fromkeys(revision_ids))
        if not ids:
            return []
        result = await db.execute(
            select(ManagedKnowledgeRevision).where(
                ManagedKnowledgeRevision.uid == uid,
                ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id,
                ManagedKnowledgeRevision.id.in_(ids),
            )
        )
        return list(result.scalars().all())

    async def create(self, db: AsyncSession, *, commit: bool = True, **values: Any) -> ManagedKnowledgeRevision:
        revision = ManagedKnowledgeRevision.model_validate(values)
        db.add(revision)
        await _finish(db, commit=commit)
        await db.refresh(revision)
        return revision

    async def list_by_knowledge_id(self, db: AsyncSession, *, uid: str, knowledge_base_id: int, knowledge_id: int, skip: int = 0, limit: int = 100) -> list[ManagedKnowledgeRevision]:
        result = await db.execute(select(ManagedKnowledgeRevision).where(ManagedKnowledgeRevision.uid == uid, ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id, ManagedKnowledgeRevision.knowledge_id == knowledge_id).order_by(ManagedKnowledgeRevision.version.desc()).offset(skip).limit(limit))
        return list(result.scalars().all())


managed_knowledge_item_crud = CRUDManagedKnowledgeItem()
managed_knowledge_revision_crud = CRUDManagedKnowledgeRevision()
