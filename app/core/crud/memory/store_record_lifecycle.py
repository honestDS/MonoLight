from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
)
from app.providers.database.time import get_database_time

from .store_common import (
    _finish,
)

__all__ = [
    "CRUDLongTermMemoryRecordLifecycle",
]


class CRUDLongTermMemoryRecordLifecycle:
    async def clear_organization_group_unique_fields(
        self,
        db: AsyncSession,
        *,
        uid: str,
        source_states: Iterable[tuple[int, int, bool, str]],
        job_id: int,
        commit: bool = True,
    ) -> bool:
        raw_states = list(source_states)
        normalized_states: dict[int, tuple[int, bool, str]] = {}
        for memory_id, version, pinned, vector_item_id in raw_states:
            if memory_id not in normalized_states:
                normalized_states[memory_id] = (version, pinned, vector_item_id)
        ordered_states = sorted(normalized_states.items())
        if not ordered_states or len(ordered_states) != len(raw_states):
            return False

        state_conditions = [
            and_(
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.version == version,
                LongTermMemoryRecord.pinned.is_(pinned),
                LongTermMemoryRecord.vector_item_id == vector_item_id,
            )
            for memory_id, (version, pinned, vector_item_id) in ordered_states
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
                LongTermMemoryRecord.pending_mutation_job_id == job_id,
                LongTermMemoryRecord.memory_key.is_not(None),
                LongTermMemoryRecord.content_hash.is_not(None),
                or_(*state_conditions),
            )
            .values(
                memory_key=None,
                content_hash=None,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == len(ordered_states)

    async def transfer_organization_source_to_cleanup(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        version: int,
        pinned: bool,
        vector_item_id: str,
        merge_job_id: int,
        cleanup_job_id: int,
        commit: bool = True,
    ) -> bool:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.is_active.is_(True),
                LongTermMemoryRecord.deleted_at.is_(None),
                LongTermMemoryRecord.index_status == LongTermMemoryRecordIndexStatus.READY,
                LongTermMemoryRecord.indexed_version == version,
                LongTermMemoryRecord.version == version,
                LongTermMemoryRecord.vector_item_id == vector_item_id,
                LongTermMemoryRecord.vector_item_id.is_not(None),
                LongTermMemoryRecord.vector_item_id != "",
                LongTermMemoryRecord.pinned.is_(pinned),
                LongTermMemoryRecord.suppress_recall.is_(False),
                LongTermMemoryRecord.pending_mutation_job_id == merge_job_id,
                LongTermMemoryRecord.memory_key.is_(None),
                LongTermMemoryRecord.content_hash.is_(None),
            )
            .values(
                is_active=False,
                deleted_at=now,
                pending_mutation_job_id=cleanup_job_id,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def suppress_for_pending_mutation(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        job_id: int,
        expected_version: int,
        commit: bool = True,
    ) -> bool:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.is_active.is_(True),
                LongTermMemoryRecord.deleted_at.is_(None),
                LongTermMemoryRecord.pending_mutation_job_id == job_id,
                LongTermMemoryRecord.version == expected_version,
            )
            .values(
                suppress_recall=True,
                suppressed_by_job_id=job_id,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def tombstone_for_pending_cleanup(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        job_id: int,
        expected_version: int,
        commit: bool = True,
    ) -> bool:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.is_active.is_(True),
                LongTermMemoryRecord.deleted_at.is_(None),
                LongTermMemoryRecord.pending_mutation_job_id == job_id,
                LongTermMemoryRecord.version == expected_version,
            )
            .values(
                is_active=False,
                deleted_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def reserve_existing_tombstone_for_cleanup(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        version: int,
        cleanup_job_id: int,
        vector_item_id: str | None = None,
        commit: bool = True,
    ) -> bool:
        conditions = [
            LongTermMemoryRecord.uid == uid,
            LongTermMemoryRecord.id == memory_id,
            LongTermMemoryRecord.version == version,
            LongTermMemoryRecord.is_active.is_(False),
            LongTermMemoryRecord.deleted_at.is_not(None),
            LongTermMemoryRecord.pending_mutation_job_id.is_(None),
        ]
        if vector_item_id is not None:
            conditions.append(LongTermMemoryRecord.vector_item_id == vector_item_id)
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(*conditions)
            .values(
                pending_mutation_job_id=cleanup_job_id,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def resume_suppressed_current(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        expected_version: int,
        suppressed_by_job_id: int,
        commit: bool = True,
    ) -> bool:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.is_active.is_(True),
                LongTermMemoryRecord.deleted_at.is_(None),
                LongTermMemoryRecord.pending_mutation_job_id.is_(None),
                LongTermMemoryRecord.version == expected_version,
                LongTermMemoryRecord.suppress_recall.is_(True),
                LongTermMemoryRecord.suppressed_by_job_id == suppressed_by_job_id,
            )
            .values(
                suppress_recall=False,
                suppressed_by_job_id=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def publish_pending_version(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        job_id: int,
        expected_version: int,
        values: dict[str, Any],
        commit: bool = True,
    ) -> LongTermMemoryRecord | None:
        allowed = {
            "memory_key",
            "memory_type",
            "content",
            "content_token_count",
            "content_hash",
            "version",
            "indexed_version",
            "vector_item_id",
            "source",
            "source_id",
            "source_session_id",
            "source_profile_id",
            "source_message_id",
            "source_job_id",
            "change_evidence",
            "is_active",
            "deleted_at",
            "suppress_recall",
            "suppressed_by_job_id",
            "index_status",
            "indexed_at",
            "pending_mutation_job_id",
        }
        update_values = {key: value for key, value in values.items() if key in allowed}
        now = await get_database_time(db)
        next_version = expected_version + 1
        update_values.update(
            {
                "version": next_version,
                "indexed_version": next_version,
                "pending_mutation_job_id": None,
                "updated_at": now,
                "indexed_at": now,
            }
        )
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.pending_mutation_job_id == job_id,
                LongTermMemoryRecord.version == expected_version,
            )
            .values(**update_values)
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

    async def finalize_deleted_tombstone(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        job_id: int,
        expected_version: int,
        commit: bool = True,
    ) -> bool:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.pending_mutation_job_id == job_id,
                LongTermMemoryRecord.version == expected_version,
                LongTermMemoryRecord.is_active.is_(False),
                LongTermMemoryRecord.deleted_at.is_not(None),
            )
            .values(
                memory_key=None,
                content_hash=None,
                content="",
                indexed_version=0,
                vector_item_id=None,
                index_status=LongTermMemoryRecordIndexStatus.READY,
                pending_mutation_job_id=None,
                suppress_recall=False,
                suppressed_by_job_id=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def delete_tombstone_after_cleanup(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
        job_id: int,
        expected_version: int,
        commit: bool = True,
    ) -> bool:
        result = await db.execute(
            delete(LongTermMemoryRecord).where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.pending_mutation_job_id == job_id,
                LongTermMemoryRecord.version == expected_version,
                LongTermMemoryRecord.is_active.is_(False),
                LongTermMemoryRecord.deleted_at.is_not(None),
            )
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

    async def delete(self, db: AsyncSession, *, uid: str, memory_id: int, commit: bool = True) -> LongTermMemoryRecord | None:
        record = await self.get_by_id(db, uid=uid, memory_id=memory_id)
        if record is None:
            return None
        result = await db.execute(delete(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.id == memory_id))
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        return record
