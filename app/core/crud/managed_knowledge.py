from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.utils.time import get_local_time
from app.models.knowledge_base import ManagedKnowledgeItem, ManagedKnowledgeRevision


async def _finish(db: AsyncSession, *, commit: bool) -> None:
    if commit:
        await db.commit()
    else:
        await db.flush()


class CRUDManagedKnowledgeItem:
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
        result = await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.uid == uid, ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id, ManagedKnowledgeItem.id.in_(ids)))
        return list(result.scalars().all())

    async def create(self, db: AsyncSession, *, commit: bool = True, **values: Any) -> ManagedKnowledgeItem:
        item = ManagedKnowledgeItem.model_validate(values)
        db.add(item)
        await _finish(db, commit=commit)
        await db.refresh(item)
        return item

    async def update_if_version(self, db: AsyncSession, *, uid: str, knowledge_base_id: int, knowledge_id: int, expected_version: int, commit: bool = True, **values: Any) -> ManagedKnowledgeItem | None:
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
            )
            .values(**update_values)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        return await self.get_by_id(db, uid=uid, knowledge_base_id=knowledge_base_id, knowledge_id=knowledge_id)

    async def tombstone_if_version(self, db: AsyncSession, *, uid: str, knowledge_base_id: int, knowledge_id: int, expected_version: int, commit: bool = True, **values: Any) -> ManagedKnowledgeItem | None:
        return await self.update_if_version(db, uid=uid, knowledge_base_id=knowledge_base_id, knowledge_id=knowledge_id, expected_version=expected_version, commit=commit, **values)


class CRUDManagedKnowledgeRevision:
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
