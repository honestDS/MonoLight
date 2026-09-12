from collections.abc import Iterable

from sqlalchemy import case, func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.memory import (
    LongTermMemoryRecord,
    LongTermMemoryType,
)
from app.providers.database.time import get_database_time

from .store_common import (
    _eviction_candidate_conditions,
    _finish,
    _memory_record_conditions,
    _memory_record_order_by,
    _organization_record_conditions,
    _recallable_conditions,
)

__all__ = [
    "CRUDLongTermMemoryRecordQuery",
]


class CRUDLongTermMemoryRecordQuery:
    async def get_by_id(self, db: AsyncSession, *, uid: str, memory_id: int) -> LongTermMemoryRecord | None:
        result = await db.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.id == memory_id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def exists_by_global_id(self, db: AsyncSession, *, memory_id: int) -> bool:
        result = await db.execute(select(LongTermMemoryRecord.id).where(LongTermMemoryRecord.id == memory_id))
        return result.scalar_one_or_none() is not None

    async def get_by_ids(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_ids: Iterable[int],
    ) -> list[LongTermMemoryRecord]:
        memory_ids = tuple(memory_ids)
        if not memory_ids:
            return []
        result = await db.execute(
            select(LongTermMemoryRecord).where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id.in_(memory_ids),
            )
        )
        return list(result.scalars().all())

    async def get_organization_group(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_ids: Iterable[int],
    ) -> list[LongTermMemoryRecord]:
        normalized_ids = sorted({memory_id for memory_id in memory_ids})
        if not normalized_ids:
            return []
        result = await db.execute(
            select(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id.in_(normalized_ids),
            )
            .order_by(LongTermMemoryRecord.id.asc())
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def list_recallable_by_ids(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_ids: Iterable[int],
    ) -> list[LongTermMemoryRecord]:
        memory_ids = tuple(memory_ids)
        if not memory_ids:
            return []
        result = await db.execute(select(LongTermMemoryRecord).where(*_recallable_conditions(uid), LongTermMemoryRecord.id.in_(memory_ids)))
        return list(result.scalars().all())

    async def list_for_organization(
        self,
        db: AsyncSession,
        *,
        uid: str,
    ) -> list[LongTermMemoryRecord]:
        result = await db.execute(select(LongTermMemoryRecord).where(*_organization_record_conditions(LongTermMemoryRecord.uid == uid)).order_by(LongTermMemoryRecord.id.asc()).execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def get_eviction_candidate(self, db: AsyncSession, *, uid: str) -> LongTermMemoryRecord | None:
        result = await db.execute(
            select(LongTermMemoryRecord)
            .where(*_eviction_candidate_conditions(uid=uid))
            .order_by(
                case((LongTermMemoryRecord.last_recalled_at.is_(None), 0), else_=1).asc(),
                LongTermMemoryRecord.last_recalled_at.asc(),
                LongTermMemoryRecord.updated_at.asc(),
                LongTermMemoryRecord.id.asc(),
            )
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def touch_last_recalled_at(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_ids: Iterable[int],
        commit: bool = True,
    ) -> int:
        memory_ids = tuple(memory_ids)
        if not memory_ids:
            return 0
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id.in_(memory_ids),
            )
            .values(last_recalled_at=now)
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return result.rowcount or 0

    async def set_pinned(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        pinned: bool,
        commit: bool = True,
    ) -> LongTermMemoryRecord | None:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.is_active.is_(True),
                LongTermMemoryRecord.deleted_at.is_(None),
            )
            .values(
                pinned=pinned,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(
            select(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
            )
            .execution_options(populate_existing=True)
        )
        return refreshed.scalars().first()

    async def get_by_key(self, db: AsyncSession, *, uid: str, memory_key: str) -> LongTermMemoryRecord | None:
        result = await db.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.memory_key == memory_key).execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_by_memory_key(self, db: AsyncSession, *, uid: str, memory_key: str) -> LongTermMemoryRecord | None:
        return await self.get_by_key(db, uid=uid, memory_key=memory_key)

    async def get_by_content_hash(self, db: AsyncSession, *, uid: str, content_hash: str) -> LongTermMemoryRecord | None:
        result = await db.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.content_hash == content_hash).execution_options(populate_existing=True))
        return result.scalars().first()

    async def list_by_uid(self, db: AsyncSession, *, uid: str, skip: int = 0, limit: int = 100) -> list[LongTermMemoryRecord]:
        result = await db.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid).order_by(LongTermMemoryRecord.id.desc()).offset(skip).limit(limit))
        return list(result.scalars().all())

    async def get_page(
        self,
        db: AsyncSession,
        *,
        uid: str,
        skip: int = 0,
        limit: int = 100,
        keyword: str | None = None,
        memory_type: LongTermMemoryType | str | None = None,
        sort_by: str | None = None,
        sort_order: str = "desc",
    ) -> list[LongTermMemoryRecord]:
        conditions = _memory_record_conditions(
            uid=uid,
            keyword=keyword,
            memory_type=memory_type,
        )
        result = await db.execute(select(LongTermMemoryRecord).where(*conditions).order_by(*_memory_record_order_by(sort_by=sort_by, sort_order=sort_order)).offset(skip).limit(limit))
        return list(result.scalars().all())

    async def count(
        self,
        db: AsyncSession,
        *,
        uid: str,
        keyword: str | None = None,
        memory_type: LongTermMemoryType | str | None = None,
    ) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(LongTermMemoryRecord)
            .where(
                *_memory_record_conditions(
                    uid=uid,
                    keyword=keyword,
                    memory_type=memory_type,
                )
            )
        )
        return int(result.scalar_one() or 0)

    async def count_active(self, db: AsyncSession, *, uid: str) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.is_active.is_(True),
                LongTermMemoryRecord.deleted_at.is_(None),
            )
        )
        return int(result.scalar_one() or 0)

    async def count_active_oversized(self, db: AsyncSession, *, uid: str, max_tokens: int) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.is_active.is_(True),
                LongTermMemoryRecord.deleted_at.is_(None),
                LongTermMemoryRecord.content_token_count > max_tokens,
            )
        )
        return int(result.scalar_one() or 0)
