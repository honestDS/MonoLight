from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
)
from app.providers.database.time import get_database_time

from .store_common import (
    _eviction_candidate_conditions,
    _finish,
    _input_data,
)

__all__ = [
    "CRUDLongTermMemoryRecordMutation",
]


class CRUDLongTermMemoryRecordMutation:
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

    async def create_pending_placeholder(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        commit: bool = True,
    ) -> LongTermMemoryRecord:
        now = await get_database_time(db)
        values = {
            "uid": uid,
            "memory_key": None,
            "content": "",
            "content_token_count": 0,
            "content_hash": None,
            "version": 0,
            "indexed_version": 0,
            "is_active": False,
            "pending_mutation_job_id": job_id,
            "index_status": LongTermMemoryRecordIndexStatus.PENDING,
            "created_at": now,
            "updated_at": now,
        }
        record = LongTermMemoryRecord.model_validate(values)
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

    async def reserve_pending_mutation(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        job_id: int,
        expected_version: int | None = None,
        commit: bool = True,
    ) -> bool:
        conditions = [
            LongTermMemoryRecord.uid == uid,
            LongTermMemoryRecord.id == memory_id,
            LongTermMemoryRecord.pending_mutation_job_id.is_(None),
        ]
        if expected_version is not None:
            conditions.append(LongTermMemoryRecord.version == expected_version)
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(*conditions)
            .values(
                pending_mutation_job_id=job_id,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def reserve_organization_group(
        self,
        db: AsyncSession,
        *,
        uid: str,
        source_versions: Iterable[tuple[int, int]],
        job_id: int,
        commit: bool = True,
    ) -> bool:
        expected_by_id: dict[int, int] = {}
        for memory_id, expected_version in source_versions:
            if memory_id not in expected_by_id:
                expected_by_id[memory_id] = expected_version
        ordered_pairs = sorted(expected_by_id.items())
        if not ordered_pairs:
            return False

        pair_conditions = [
            and_(
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.version == expected_version,
            )
            for memory_id, expected_version in ordered_pairs
        ]
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.is_active.is_(True),
                LongTermMemoryRecord.deleted_at.is_(None),
                LongTermMemoryRecord.index_status == LongTermMemoryRecordIndexStatus.READY,
                LongTermMemoryRecord.indexed_version == LongTermMemoryRecord.version,
                LongTermMemoryRecord.vector_item_id.is_not(None),
                LongTermMemoryRecord.vector_item_id != "",
                LongTermMemoryRecord.suppress_recall.is_(False),
                LongTermMemoryRecord.pending_mutation_job_id.is_(None),
                or_(*pair_conditions),
            )
            .values(
                pending_mutation_job_id=job_id,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == len(ordered_pairs)

    async def reserve_eviction_candidate(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        version: int,
        vector_item_id: str,
        job_id: int,
        commit: bool = True,
    ) -> bool:
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                *_eviction_candidate_conditions(
                    uid=uid,
                    memory_id=memory_id,
                    version=version,
                    vector_item_id=vector_item_id,
                )
            )
            .values(
                pending_mutation_job_id=job_id,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def transfer_eviction_candidate_to_cleanup(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        version: int,
        vector_item_id: str,
        replacement_job_id: int,
        cleanup_job_id: int,
        commit: bool = True,
    ) -> bool:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                *_eviction_candidate_conditions(
                    uid=uid,
                    memory_id=memory_id,
                    version=version,
                    vector_item_id=vector_item_id,
                    pending_job_id=replacement_job_id,
                )
            )
            .values(
                pending_mutation_job_id=cleanup_job_id,
                is_active=False,
                deleted_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1
