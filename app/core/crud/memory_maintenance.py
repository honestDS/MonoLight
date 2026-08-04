from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_MEMORY_JOB_FIELD_REQUIRED, ERR_MEMORY_JOB_OWNER_MISMATCH
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryOldCollectionCleanupStatus,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryStore,
)
from app.providers.database.time import get_database_time

_TERMINAL_MIGRATION_STATUSES = (
    LongTermMemoryMigrationStatus.SUCCEEDED,
    LongTermMemoryMigrationStatus.FAILED,
    LongTermMemoryMigrationStatus.CANCELLED,
)
_BLOCKING_CLEANUP_STATUSES = (
    LongTermMemoryOldCollectionCleanupStatus.PENDING,
    LongTermMemoryOldCollectionCleanupStatus.RUNNING,
    LongTermMemoryOldCollectionCleanupStatus.FAILED,
)
_MIGRATION_PRE_SWITCH_STATUSES = (
    LongTermMemoryMigrationStatus.PREPARING,
    LongTermMemoryMigrationStatus.BUILDING,
    LongTermMemoryMigrationStatus.CATCHING_UP,
    LongTermMemoryMigrationStatus.VALIDATING,
)


async def _finish(db: AsyncSession, *, commit: bool) -> None:
    if commit:
        await db.commit()
    else:
        await db.flush()


def _recallable_conditions(uid: str) -> list[Any]:
    return [
        LongTermMemoryRecord.uid == uid,
        LongTermMemoryRecord.is_active.is_(True),
        LongTermMemoryRecord.deleted_at.is_(None),
        LongTermMemoryRecord.suppress_recall.is_(False),
        LongTermMemoryRecord.index_status == LongTermMemoryRecordIndexStatus.READY,
        LongTermMemoryRecord.indexed_version == LongTermMemoryRecord.version,
        LongTermMemoryRecord.vector_item_id.is_not(None),
        LongTermMemoryRecord.vector_item_id != "",
    ]


def _cleanup_available_condition() -> Any:
    return or_(
        LongTermMemoryStore.old_collection_cleanup_status.is_(None),
        LongTermMemoryStore.old_collection_cleanup_status.notin_(_BLOCKING_CLEANUP_STATUSES),
    )


def _resolve_owner(owner: str | None, worker_id: str | None) -> str:
    if owner is None:
        owner = worker_id
    elif worker_id is not None and worker_id != owner:
        raise ValueError(t(ERR_MEMORY_JOB_OWNER_MISMATCH))
    if owner is None:
        raise TypeError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="owner"))
    if not owner:
        raise ValueError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="owner"))
    return owner


class CRUDLongTermMemoryMaintenanceStore:
    async def start_reindex(
        self,
        db: AsyncSession,
        *,
        uid: str,
        expected_active_revision: int,
        expected_active_collection_name: str,
        expected_index_revision: int,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.active_embedding_revision == expected_active_revision,
                LongTermMemoryStore.active_collection_name == expected_active_collection_name,
                LongTermMemoryStore.index_revision == expected_index_revision,
                LongTermMemoryStore.index_status != LongTermMemoryIndexStatus.REINDEXING,
                _cleanup_available_condition(),
                or_(
                    LongTermMemoryStore.migration_job_id.is_(None),
                    LongTermMemoryStore.migration_status.in_(_TERMINAL_MIGRATION_STATUSES),
                ),
            )
            .values(
                index_status=LongTermMemoryIndexStatus.REINDEXING,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def update_embedding_migration_progress(
        self,
        db: AsyncSession,
        *,
        uid: str,
        migration_job_id: int,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> LongTermMemoryStore | None:
        data: dict[str, Any] = {}
        if obj_in is not None:
            if isinstance(obj_in, dict):
                data.update(obj_in)
            else:
                data.update(obj_in.model_dump(exclude_unset=True))
        data.update(values)
        allowed = {
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
        }
        update_values: dict[str, Any] = {}
        for key, value in data.items():
            if key in allowed:
                update_values[key] = value
        update_values["updated_at"] = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.migration_job_id == migration_job_id,
            )
            .values(**update_values)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def advance_migration_delta_applied_watermark(
        self,
        db: AsyncSession,
        *,
        uid: str,
        migration_job_id: int,
        expected_watermark: int,
        new_watermark: int,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.migration_job_id == migration_job_id,
                LongTermMemoryStore.migration_delta_applied_watermark == expected_watermark,
            )
            .values(
                migration_delta_applied_watermark=new_watermark,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def complete_reindex_switch(
        self,
        db: AsyncSession,
        *,
        uid: str,
        expected_active_revision: int,
        expected_active_collection_name: str,
        expected_index_revision: int,
        target_collection_name: str,
        target_index_revision: int,
        old_collection_cleanup_job_id: int,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.active_embedding_revision == expected_active_revision,
                LongTermMemoryStore.active_collection_name == expected_active_collection_name,
                LongTermMemoryStore.index_revision == expected_index_revision,
                LongTermMemoryStore.index_status == LongTermMemoryIndexStatus.REINDEXING,
            )
            .values(
                active_collection_name=target_collection_name,
                index_revision=target_index_revision,
                index_status=LongTermMemoryIndexStatus.READY,
                old_collection_name=expected_active_collection_name,
                old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.PENDING,
                old_collection_cleanup_job_id=old_collection_cleanup_job_id,
                old_collection_cleanup_error=None,
                old_collection_cleanup_at=None,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def complete_embedding_migration_switch(
        self,
        db: AsyncSession,
        *,
        uid: str,
        migration_job_id: int,
        expected_active_channel_id: int,
        expected_active_model_id: str,
        expected_active_dimensions: int,
        expected_active_signature: str,
        expected_active_revision: int,
        expected_active_collection_name: str,
        expected_index_revision: int,
        target_channel_id: int,
        target_model_id: str,
        target_dimensions: int,
        target_signature: str,
        target_revision: int,
        target_index_revision: int,
        target_collection_name: str,
        old_collection_cleanup_job_id: int,
        finished_at: datetime,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.migration_job_id == migration_job_id,
                LongTermMemoryStore.migration_status == LongTermMemoryMigrationStatus.SWITCHING,
                LongTermMemoryStore.active_embedding_channel_id == expected_active_channel_id,
                LongTermMemoryStore.active_embedding_model_id == expected_active_model_id,
                LongTermMemoryStore.active_embedding_dimensions == expected_active_dimensions,
                LongTermMemoryStore.active_embedding_signature == expected_active_signature,
                LongTermMemoryStore.active_embedding_revision == expected_active_revision,
                LongTermMemoryStore.active_collection_name == expected_active_collection_name,
                LongTermMemoryStore.index_revision == expected_index_revision,
                LongTermMemoryStore.target_embedding_channel_id == target_channel_id,
                LongTermMemoryStore.target_embedding_model_id == target_model_id,
                LongTermMemoryStore.target_embedding_dimensions == target_dimensions,
                LongTermMemoryStore.target_embedding_signature == target_signature,
                LongTermMemoryStore.target_collection_name == target_collection_name,
            )
            .values(
                active_embedding_channel_id=target_channel_id,
                active_embedding_model_id=target_model_id,
                active_embedding_dimensions=target_dimensions,
                active_embedding_signature=target_signature,
                active_embedding_revision=target_revision,
                active_collection_name=target_collection_name,
                index_revision=target_index_revision,
                index_status=LongTermMemoryIndexStatus.READY,
                target_embedding_channel_id=None,
                target_embedding_model_id=None,
                target_embedding_dimensions=None,
                target_embedding_signature=None,
                target_collection_name=None,
                migration_status=LongTermMemoryMigrationStatus.SUCCEEDED,
                migration_error=None,
                migration_finished_at=finished_at,
                old_collection_name=expected_active_collection_name,
                old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.PENDING,
                old_collection_cleanup_job_id=old_collection_cleanup_job_id,
                old_collection_cleanup_error=None,
                old_collection_cleanup_at=None,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def update_old_collection_cleanup(
        self,
        db: AsyncSession,
        *,
        uid: str,
        old_collection_cleanup_job_id: int,
        old_collection_cleanup_status: LongTermMemoryOldCollectionCleanupStatus,
        old_collection_cleanup_error: str | None = None,
        old_collection_cleanup_at: datetime | None = None,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.old_collection_cleanup_job_id == old_collection_cleanup_job_id,
            )
            .values(
                old_collection_cleanup_status=old_collection_cleanup_status,
                old_collection_cleanup_error=old_collection_cleanup_error,
                old_collection_cleanup_at=old_collection_cleanup_at,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def restart_old_collection_cleanup(
        self,
        db: AsyncSession,
        *,
        uid: str,
        expected_cleanup_job_id: int,
        retry_cleanup_job_id: int,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.old_collection_cleanup_job_id == expected_cleanup_job_id,
                LongTermMemoryStore.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.FAILED,
                LongTermMemoryStore.old_collection_name.is_not(None),
                LongTermMemoryStore.old_collection_name != "",
            )
            .values(
                old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.PENDING,
                old_collection_cleanup_job_id=retry_cleanup_job_id,
                old_collection_cleanup_error=None,
                old_collection_cleanup_at=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def mark_target_collection_cleanup_failed(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        operation: LongTermMemoryMutationOperation | str,
        expected_active_collection_name: str,
        target_collection_name: str,
        error: str,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        try:
            operation = LongTermMemoryMutationOperation(operation)
        except (TypeError, ValueError):
            return None
        conditions: list[Any] = [
            LongTermMemoryStore.uid == uid,
            LongTermMemoryStore.active_collection_name == expected_active_collection_name,
        ]
        if operation == LongTermMemoryMutationOperation.REINDEX:
            conditions.append(LongTermMemoryStore.index_status == LongTermMemoryIndexStatus.REINDEXING)
        elif operation == LongTermMemoryMutationOperation.EMBEDDING_MIGRATION:
            conditions.extend(
                [
                    LongTermMemoryStore.migration_job_id == job_id,
                    LongTermMemoryStore.migration_status.in_(_MIGRATION_PRE_SWITCH_STATUSES),
                ]
            )
        else:
            return None
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(*conditions)
            .values(
                old_collection_name=target_collection_name,
                old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.FAILED,
                old_collection_cleanup_job_id=job_id,
                old_collection_cleanup_error=error,
                old_collection_cleanup_at=None,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()


class CRUDLongTermMemoryMaintenanceRecord:
    async def get_recallable_snapshot(self, db: AsyncSession, *, uid: str) -> tuple[int, int]:
        result = await db.execute(select(func.max(LongTermMemoryRecord.id), func.count()).select_from(LongTermMemoryRecord).where(*_recallable_conditions(uid)))
        boundary, count = result.one()
        return int(boundary or 0), int(count or 0)

    async def list_recallable_page(
        self,
        db: AsyncSession,
        *,
        uid: str,
        after_id: int,
        boundary: int,
        limit: int = 100,
    ) -> list[LongTermMemoryRecord]:
        result = await db.execute(
            select(LongTermMemoryRecord)
            .where(
                *_recallable_conditions(uid),
                LongTermMemoryRecord.id > after_id,
                LongTermMemoryRecord.id <= boundary,
            )
            .order_by(LongTermMemoryRecord.id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_recallable_by_memory_id(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int,
    ) -> LongTermMemoryRecord | None:
        conditions = _recallable_conditions(uid)
        conditions.append(LongTermMemoryRecord.id == memory_id)
        result = await db.execute(select(LongTermMemoryRecord).where(*conditions).execution_options(populate_existing=True))
        return result.scalars().first()


class CRUDLongTermMemoryMaintenanceJob:
    async def update_running_payload(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        payload: dict[str, Any],
        owner: str | None = None,
        worker_id: str | None = None,
        require_cancel_not_requested: bool = True,
        commit: bool = True,
    ) -> LongTermMemoryMutationJob | None:
        owner = _resolve_owner(owner, worker_id)
        now = await get_database_time(db)
        conditions: list[Any] = [
            LongTermMemoryMutationJob.uid == uid,
            LongTermMemoryMutationJob.id == job_id,
            LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
            LongTermMemoryMutationJob.locked_by == owner,
            LongTermMemoryMutationJob.lock_until >= now,
        ]
        if require_cancel_not_requested:
            conditions.append(LongTermMemoryMutationJob.cancel_requested_at.is_(None))
        result = await db.execute(update(LongTermMemoryMutationJob).where(*conditions).values(payload=payload, updated_at=now).execution_options(synchronize_session=False))
        await _finish(db, commit=commit)
        if (result.rowcount or 0) != 1:
            return None
        refreshed = await db.execute(
            select(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
            )
            .execution_options(populate_existing=True)
        )
        return refreshed.scalars().first()

    async def update_terminal_payload(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        payload: dict[str, Any],
        commit: bool = True,
    ) -> LongTermMemoryMutationJob | None:
        result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
                LongTermMemoryMutationJob.status.in_(
                    [
                        LongTermMemoryMutationStatus.FAILED,
                        LongTermMemoryMutationStatus.CANCELLED,
                    ]
                ),
            )
            .values(
                payload=payload,
                updated_at=await get_database_time(db),
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(
            select(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
            )
            .execution_options(populate_existing=True)
        )
        return refreshed.scalars().first()


memory_maintenance_store_crud = CRUDLongTermMemoryMaintenanceStore()
memory_maintenance_record_crud = CRUDLongTermMemoryMaintenanceRecord()
memory_maintenance_job_crud = CRUDLongTermMemoryMaintenanceJob()


__all__ = [
    "CRUDLongTermMemoryMaintenanceJob",
    "CRUDLongTermMemoryMaintenanceRecord",
    "CRUDLongTermMemoryMaintenanceStore",
    "memory_maintenance_job_crud",
    "memory_maintenance_record_crud",
    "memory_maintenance_store_crud",
]
