from typing import Any

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryMutationJob,
    LongTermMemoryRecord,
    LongTermMemoryRevision,
    LongTermMemoryStore,
)

from .store_common import _finish, _input_data, _organization_record_conditions

__all__ = [
    "CRUDLongTermMemoryRevision",
    "CRUDLongTermMemoryEmbeddingRevision",
    "CRUDLongTermMemoryEmbeddingDelta",
    "CRUDLongTermMemoryReference",
]


class CRUDLongTermMemoryRevision:
    async def get_by_memory_id(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        version: int | None = None,
    ) -> LongTermMemoryRevision | None:
        stmt = select(LongTermMemoryRevision).where(LongTermMemoryRevision.uid == uid, LongTermMemoryRevision.memory_id == memory_id)
        if version is not None:
            stmt = stmt.where(LongTermMemoryRevision.version == version)
        else:
            stmt = stmt.order_by(LongTermMemoryRevision.version.desc())
        result = await db.execute(stmt)
        return result.scalars().first()

    async def list_by_memory_id(self, db: AsyncSession, *, uid: str, memory_id: int, skip: int = 0, limit: int = 100) -> list[LongTermMemoryRevision]:
        result = await db.execute(select(LongTermMemoryRevision).where(LongTermMemoryRevision.uid == uid, LongTermMemoryRevision.memory_id == memory_id).order_by(LongTermMemoryRevision.version.desc()).offset(skip).limit(limit))
        return list(result.scalars().all())

    async def count_by_memory_id(self, db: AsyncSession, *, uid: str, memory_id: int) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(LongTermMemoryRevision)
            .where(
                LongTermMemoryRevision.uid == uid,
                LongTermMemoryRevision.memory_id == memory_id,
            )
        )
        return int(result.scalar_one() or 0)

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        version: int,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> LongTermMemoryRevision:
        data = _input_data(obj_in)
        data.pop("uid", None)
        data.update(values)
        revision = LongTermMemoryRevision.model_validate({"uid": uid, "memory_id": memory_id, "version": version, **data})
        db.add(revision)
        await _finish(db, commit=commit)
        await db.refresh(revision)
        return revision

    async def write(self, db: AsyncSession, **kwargs: Any) -> LongTermMemoryRevision:
        return await self.create(db, **kwargs)


class CRUDLongTermMemoryEmbeddingRevision:
    async def get_by_revision(self, db: AsyncSession, *, uid: str, revision: int) -> LongTermMemoryEmbeddingRevision | None:
        result = await db.execute(select(LongTermMemoryEmbeddingRevision).where(LongTermMemoryEmbeddingRevision.uid == uid, LongTermMemoryEmbeddingRevision.revision == revision))
        return result.scalars().first()

    async def get_by_job_id(self, db: AsyncSession, *, uid: str, job_id: int) -> LongTermMemoryEmbeddingRevision | None:
        result = await db.execute(
            select(LongTermMemoryEmbeddingRevision)
            .where(
                LongTermMemoryEmbeddingRevision.uid == uid,
                LongTermMemoryEmbeddingRevision.job_id == job_id,
            )
            .order_by(LongTermMemoryEmbeddingRevision.revision.desc())
        )
        return result.scalars().first()

    async def count_by_uid(self, db: AsyncSession, *, uid: str) -> int:
        result = await db.execute(select(func.count()).select_from(LongTermMemoryEmbeddingRevision).where(LongTermMemoryEmbeddingRevision.uid == uid))
        return int(result.scalar_one() or 0)

    async def get_next_revision(self, db: AsyncSession, *, uid: str) -> int:
        result = await db.execute(select(func.max(LongTermMemoryEmbeddingRevision.revision)).where(LongTermMemoryEmbeddingRevision.uid == uid))
        return int(result.scalar() or 0) + 1

    async def list_by_uid(
        self,
        db: AsyncSession,
        *,
        uid: str,
        skip: int = 0,
        limit: int = 100,
    ) -> list[LongTermMemoryEmbeddingRevision]:
        result = await db.execute(select(LongTermMemoryEmbeddingRevision).where(LongTermMemoryEmbeddingRevision.uid == uid).order_by(LongTermMemoryEmbeddingRevision.revision.desc()).offset(skip).limit(limit))
        return list(result.scalars().all())

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        revision: int,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> LongTermMemoryEmbeddingRevision:
        data = _input_data(obj_in)
        data.pop("uid", None)
        data.update(values)
        embedding_revision = LongTermMemoryEmbeddingRevision.model_validate({"uid": uid, "revision": revision, **data})
        db.add(embedding_revision)
        await _finish(db, commit=commit)
        await db.refresh(embedding_revision)
        return embedding_revision

    async def write(self, db: AsyncSession, **kwargs: Any) -> LongTermMemoryEmbeddingRevision:
        return await self.create(db, **kwargs)

    async def update_by_revision(
        self,
        db: AsyncSession,
        *,
        uid: str,
        revision: int,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> LongTermMemoryEmbeddingRevision | None:
        data = _input_data(obj_in)
        data.update(values)
        allowed = {
            "confirmation_source_profile_id",
            "confirmation_source",
            "embedding_selection_signature",
            "confirmed_at",
            "job_id",
            "status",
            "result",
            "error",
            "started_at",
            "finished_at",
        }
        update_values = {key: value for key, value in data.items() if key in allowed}
        update_values["updated_at"] = get_local_time()
        result = await db.execute(
            update(LongTermMemoryEmbeddingRevision)
            .where(
                LongTermMemoryEmbeddingRevision.uid == uid,
                LongTermMemoryEmbeddingRevision.revision == revision,
            )
            .values(**update_values)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(
            select(LongTermMemoryEmbeddingRevision)
            .where(
                LongTermMemoryEmbeddingRevision.uid == uid,
                LongTermMemoryEmbeddingRevision.revision == revision,
            )
            .execution_options(populate_existing=True)
        )
        return refreshed.scalars().first()


class CRUDLongTermMemoryEmbeddingDelta:
    async def list_by_migration_job(
        self,
        db: AsyncSession,
        *,
        uid: str,
        migration_job_id: int,
        sequence_start: int | None = None,
        sequence_end: int | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[LongTermMemoryEmbeddingDelta]:
        conditions = [
            LongTermMemoryEmbeddingDelta.uid == uid,
            LongTermMemoryEmbeddingDelta.migration_job_id == migration_job_id,
        ]
        if sequence_start is not None:
            conditions.append(LongTermMemoryEmbeddingDelta.sequence >= sequence_start)
        if sequence_end is not None:
            conditions.append(LongTermMemoryEmbeddingDelta.sequence <= sequence_end)
        result = await db.execute(select(LongTermMemoryEmbeddingDelta).where(*conditions).order_by(LongTermMemoryEmbeddingDelta.sequence).offset(skip).limit(limit))
        return list(result.scalars().all())

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        migration_job_id: int,
        sequence: int,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> LongTermMemoryEmbeddingDelta:
        data = _input_data(obj_in)
        data.pop("uid", None)
        data.update(values)
        delta = LongTermMemoryEmbeddingDelta.model_validate({"uid": uid, "migration_job_id": migration_job_id, "sequence": sequence, **data})
        db.add(delta)
        await _finish(db, commit=commit)
        await db.refresh(delta)
        return delta

    async def write(self, db: AsyncSession, **kwargs: Any) -> LongTermMemoryEmbeddingDelta:
        return await self.create(db, **kwargs)

    async def update_by_sequence(
        self,
        db: AsyncSession,
        *,
        uid: str,
        migration_job_id: int,
        sequence: int,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> LongTermMemoryEmbeddingDelta | None:
        data = _input_data(obj_in)
        data.update(values)
        allowed = {"status", "error", "applied_at"}
        update_values = {key: value for key, value in data.items() if key in allowed}
        result = await db.execute(
            update(LongTermMemoryEmbeddingDelta)
            .where(
                LongTermMemoryEmbeddingDelta.uid == uid,
                LongTermMemoryEmbeddingDelta.migration_job_id == migration_job_id,
                LongTermMemoryEmbeddingDelta.sequence == sequence,
            )
            .values(**update_values)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(
            select(LongTermMemoryEmbeddingDelta)
            .where(
                LongTermMemoryEmbeddingDelta.uid == uid,
                LongTermMemoryEmbeddingDelta.migration_job_id == migration_job_id,
                LongTermMemoryEmbeddingDelta.sequence == sequence,
            )
            .execution_options(populate_existing=True)
        )
        return refreshed.scalars().first()

    async def update_status(self, db: AsyncSession, **kwargs: Any) -> LongTermMemoryEmbeddingDelta | None:
        return await self.update_by_sequence(db, **kwargs)

    async def get_high_water_sequence(self, db: AsyncSession, *, uid: str, migration_job_id: int) -> int:
        result = await db.execute(
            select(func.max(LongTermMemoryEmbeddingDelta.sequence)).where(
                LongTermMemoryEmbeddingDelta.uid == uid,
                LongTermMemoryEmbeddingDelta.migration_job_id == migration_job_id,
            )
        )
        return int(result.scalar() or 0)


class CRUDLongTermMemoryReference:
    """管理员保护检查使用的长期记忆基础数据读取。"""

    async def list_organization_records_by_uids(self, db: AsyncSession, *, uids: set[str]) -> dict[str, list[LongTermMemoryRecord]]:
        if not uids:
            return {}
        result = await db.execute(select(LongTermMemoryRecord).where(*_organization_record_conditions(LongTermMemoryRecord.uid.in_(uids))).order_by(LongTermMemoryRecord.uid.asc(), LongTermMemoryRecord.id.asc()))
        records_by_uid: dict[str, list[LongTermMemoryRecord]] = {}
        for record in result.scalars().all():
            records_by_uid.setdefault(record.uid, []).append(record)
        return records_by_uid

    async def list_all_stores_for_admin(self, db: AsyncSession) -> list[LongTermMemoryStore]:
        result = await db.execute(select(LongTermMemoryStore).order_by(LongTermMemoryStore.uid))
        return list(result.scalars().all())

    async def list_all_embedding_revisions_for_admin(self, db: AsyncSession) -> list[LongTermMemoryEmbeddingRevision]:
        result = await db.execute(
            select(LongTermMemoryEmbeddingRevision).order_by(
                LongTermMemoryEmbeddingRevision.uid,
                LongTermMemoryEmbeddingRevision.revision.desc(),
            )
        )
        return list(result.scalars().all())

    async def list_all_memory_jobs_for_admin(self, db: AsyncSession) -> list[LongTermMemoryMutationJob]:
        result = await db.execute(
            select(LongTermMemoryMutationJob).order_by(
                LongTermMemoryMutationJob.uid,
                LongTermMemoryMutationJob.id,
            )
        )
        return list(result.scalars().all())
