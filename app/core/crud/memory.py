from typing import Any

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryRecord,
    LongTermMemoryRevision,
    LongTermMemoryStore,
)


def _input_data(obj_in: Any) -> dict[str, Any]:
    if obj_in is None:
        return {}
    if isinstance(obj_in, dict):
        return dict(obj_in)
    return obj_in.model_dump(exclude_unset=True)


async def _finish(db: AsyncSession, *, commit: bool) -> None:
    if commit:
        await db.commit()
    else:
        await db.flush()


class CRUDLongTermMemoryStore:
    async def get_by_uid(self, db: AsyncSession, *, uid: str) -> LongTermMemoryStore | None:
        result = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid))
        return result.scalars().first()

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> LongTermMemoryStore:
        data = _input_data(obj_in)
        data.pop("uid", None)
        data.update(values)
        store = LongTermMemoryStore.model_validate({"uid": uid, **data})
        db.add(store)
        await _finish(db, commit=commit)
        await db.refresh(store)
        return store

    async def update_by_uid(
        self,
        db: AsyncSession,
        *,
        uid: str,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> LongTermMemoryStore | None:
        data = _input_data(obj_in)
        data.update(values)
        allowed = {
            "active_embedding_channel_id",
            "active_embedding_model_id",
            "active_embedding_dimensions",
            "active_embedding_signature",
            "active_embedding_revision",
            "active_collection_name",
            "target_embedding_channel_id",
            "target_embedding_model_id",
            "target_embedding_dimensions",
            "target_embedding_signature",
            "target_collection_name",
            "migration_job_id",
            "migration_status",
            "migration_snapshot_boundary",
            "migration_cursor",
            "migration_total_count",
            "migration_success_count",
            "migration_failure_count",
            "migration_delta_high_watermark",
            "migration_delta_applied_watermark",
            "migration_error",
            "migration_started_at",
            "migration_finished_at",
            "old_collection_name",
            "old_collection_cleanup_status",
            "old_collection_cleanup_job_id",
            "old_collection_cleanup_error",
            "old_collection_cleanup_at",
            "max_active_records",
            "index_revision",
            "index_status",
        }
        update_values = {key: value for key, value in data.items() if key in allowed}
        update_values["updated_at"] = get_local_time()
        result = await db.execute(update(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).values(**update_values).execution_options(synchronize_session=False))
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def get_or_create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> tuple[LongTermMemoryStore, bool]:
        existing = await self.get_by_uid(db, uid=uid)
        if existing is not None:
            return existing, False
        data = _input_data(obj_in)
        data.pop("uid", None)
        data.update(values)
        store = LongTermMemoryStore.model_validate({"uid": uid, **data})
        try:
            async with db.begin_nested():
                db.add(store)
                await db.flush()
        except IntegrityError:
            existing = await self.get_by_uid(db, uid=uid)
            if existing is None:
                raise
            return existing, False
        if commit:
            await db.commit()
        await db.refresh(store)
        return store, True


class CRUDLongTermMemoryRecord:
    async def get_by_id(self, db: AsyncSession, *, uid: str, memory_id: int) -> LongTermMemoryRecord | None:
        result = await db.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.id == memory_id))
        return result.scalars().first()

    async def get_by_key(self, db: AsyncSession, *, uid: str, memory_key: str) -> LongTermMemoryRecord | None:
        result = await db.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.memory_key == memory_key))
        return result.scalars().first()

    async def get_by_memory_key(self, db: AsyncSession, *, uid: str, memory_key: str) -> LongTermMemoryRecord | None:
        return await self.get_by_key(db, uid=uid, memory_key=memory_key)

    async def get_by_content_hash(self, db: AsyncSession, *, uid: str, content_hash: str) -> LongTermMemoryRecord | None:
        result = await db.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.content_hash == content_hash))
        return result.scalars().first()

    async def list_by_uid(self, db: AsyncSession, *, uid: str, skip: int = 0, limit: int = 100) -> list[LongTermMemoryRecord]:
        result = await db.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid).order_by(LongTermMemoryRecord.id.desc()).offset(skip).limit(limit))
        return list(result.scalars().all())

    async def get_page(self, db: AsyncSession, *, uid: str, skip: int = 0, limit: int = 100) -> list[LongTermMemoryRecord]:
        return await self.list_by_uid(db, uid=uid, skip=skip, limit=limit)

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

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> LongTermMemoryRecord:
        data = _input_data(obj_in)
        data.pop("uid", None)
        data.update(values)
        record = LongTermMemoryRecord.model_validate({"uid": uid, **data})
        db.add(record)
        await _finish(db, commit=commit)
        await db.refresh(record)
        return record

    async def update_if_version(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        expected_version: int,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> LongTermMemoryRecord | None:
        data = _input_data(obj_in)
        data.update(values)
        for key in ("id", "uid", "version", "created_at"):
            data.pop(key, None)
        data["version"] = LongTermMemoryRecord.version + 1
        data["updated_at"] = get_local_time()
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.version == expected_version,
            )
            .values(**data)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.id == memory_id).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def update_expected_version(self, db: AsyncSession, **kwargs: Any) -> LongTermMemoryRecord | None:
        return await self.update_if_version(db, **kwargs)

    async def delete(self, db: AsyncSession, *, uid: str, memory_id: int, commit: bool = True) -> LongTermMemoryRecord | None:
        record = await self.get_by_id(db, uid=uid, memory_id=memory_id)
        if record is None:
            return None
        result = await db.execute(delete(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.id == memory_id))
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        return record


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


memory_store_crud = CRUDLongTermMemoryStore()
memory_record_crud = CRUDLongTermMemoryRecord()
memory_revision_crud = CRUDLongTermMemoryRevision()
memory_embedding_revision_crud = CRUDLongTermMemoryEmbeddingRevision()
memory_embedding_delta_crud = CRUDLongTermMemoryEmbeddingDelta()

long_term_memory_store_crud = memory_store_crud
long_term_memory_record_crud = memory_record_crud
long_term_memory_revision_crud = memory_revision_crud
long_term_memory_embedding_revision_crud = memory_embedding_revision_crud
long_term_memory_embedding_delta_crud = memory_embedding_delta_crud
