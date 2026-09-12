from datetime import datetime
from typing import Any

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryEmbeddingSelectionToken,
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryOldCollectionCleanupStatus,
    LongTermMemoryStore,
)
from app.providers.database.time import get_database_time

from .store_common import _finish, _input_data

__all__ = [
    "CRUDLongTermMemoryStore",
    "CRUDLongTermMemoryEmbeddingSelectionToken",
]


class CRUDLongTermMemoryStore:
    async def get_by_uid(self, db: AsyncSession, *, uid: str) -> LongTermMemoryStore | None:
        result = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_snapshot_by_uid(self, db: AsyncSession, *, uid: str) -> LongTermMemoryStore | None:
        result = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return result.scalars().first()

    async def lock_for_mutation(
        self,
        db: AsyncSession,
        *,
        uid: str,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        result = await db.execute(update(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).values(updated_at=LongTermMemoryStore.updated_at).execution_options(synchronize_session=False))
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def get_multi_by_uids(self, db: AsyncSession, *, uids: set[str]) -> dict[str, LongTermMemoryStore]:
        if not uids:
            return {}
        result = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid.in_(uids)).execution_options(populate_existing=True))
        return {store.uid: store for store in result.scalars().all()}

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
            "organize_trigger_records",
            "auto_organize_enabled",
            "organization_channel_id",
            "organization_model_id",
            "organization_policy_version",
            "organization_last_job_id",
            "organization_last_run_at",
            "organization_error",
            "capacity_status",
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

    async def update_auto_organize_if_channel_and_model(
        self,
        db: AsyncSession,
        *,
        uid: str,
        expected_channel_id: int | None,
        expected_model_id: str | None,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> bool:
        data = _input_data(obj_in)
        data.update(values)
        allowed = {
            "auto_organize_enabled",
            "organization_channel_id",
            "organization_model_id",
        }
        update_values = {key: value for key, value in data.items() if key in allowed}
        update_values["updated_at"] = get_local_time()
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.organization_channel_id == expected_channel_id,
                LongTermMemoryStore.organization_model_id == expected_model_id,
            )
            .values(**update_values)
            .execution_options(synchronize_session=False)
        )
        await _finish(db, commit=commit)
        return (result.rowcount or 0) == 1

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

    async def activate_initial_embedding_if_unconfigured(
        self,
        db: AsyncSession,
        *,
        uid: str,
        expected_active_revision: int,
        active_embedding_channel_id: int,
        active_embedding_model_id: str,
        active_embedding_dimensions: int,
        active_embedding_signature: str,
        active_collection_name: str,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.active_embedding_revision == expected_active_revision,
                LongTermMemoryStore.active_embedding_channel_id.is_(None),
            )
            .values(
                active_embedding_channel_id=active_embedding_channel_id,
                active_embedding_model_id=active_embedding_model_id,
                active_embedding_dimensions=active_embedding_dimensions,
                active_embedding_signature=active_embedding_signature,
                active_embedding_revision=expected_active_revision + 1,
                active_collection_name=active_collection_name,
                target_embedding_channel_id=None,
                target_embedding_model_id=None,
                target_embedding_dimensions=None,
                target_embedding_signature=None,
                target_collection_name=None,
                migration_job_id=None,
                migration_status=None,
                index_status="pending",
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        return await self.get_snapshot_by_uid(db, uid=uid)

    async def start_embedding_migration(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        expected_active_revision: int,
        target_embedding_channel_id: int,
        target_embedding_model_id: str,
        target_embedding_dimensions: int,
        target_embedding_signature: str,
        target_collection_name: str,
        migration_started_at: datetime,
        commit: bool = True,
    ) -> LongTermMemoryStore | None:
        terminal_statuses = [
            LongTermMemoryMigrationStatus.SUCCEEDED.value,
            LongTermMemoryMigrationStatus.FAILED.value,
            LongTermMemoryMigrationStatus.CANCELLED.value,
        ]
        blocked_cleanup_statuses = [
            LongTermMemoryOldCollectionCleanupStatus.PENDING,
            LongTermMemoryOldCollectionCleanupStatus.RUNNING,
            LongTermMemoryOldCollectionCleanupStatus.FAILED,
        ]
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.active_embedding_revision == expected_active_revision,
                LongTermMemoryStore.index_status != LongTermMemoryIndexStatus.REINDEXING,
                or_(
                    LongTermMemoryStore.old_collection_cleanup_status.is_(None),
                    LongTermMemoryStore.old_collection_cleanup_status.notin_(blocked_cleanup_statuses),
                ),
                or_(
                    LongTermMemoryStore.migration_job_id.is_(None),
                    LongTermMemoryStore.migration_status.in_(terminal_statuses),
                ),
            )
            .values(
                target_embedding_channel_id=target_embedding_channel_id,
                target_embedding_model_id=target_embedding_model_id,
                target_embedding_dimensions=target_embedding_dimensions,
                target_embedding_signature=target_embedding_signature,
                target_collection_name=target_collection_name,
                migration_job_id=job_id,
                migration_status=LongTermMemoryMigrationStatus.PREPARING,
                migration_snapshot_boundary=0,
                migration_cursor=0,
                migration_total_count=0,
                migration_success_count=0,
                migration_failure_count=0,
                migration_delta_high_watermark=0,
                migration_delta_applied_watermark=0,
                migration_error=None,
                migration_started_at=migration_started_at,
                migration_finished_at=None,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def reserve_migration_delta_sequence(
        self,
        db: AsyncSession,
        *,
        uid: str,
        migration_job_id: int,
        expected_high_watermark: int,
        commit: bool = True,
    ) -> int | None:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.migration_job_id == migration_job_id,
                LongTermMemoryStore.migration_delta_high_watermark == expected_high_watermark,
                LongTermMemoryStore.migration_status.in_(
                    [
                        LongTermMemoryMigrationStatus.PREPARING,
                        LongTermMemoryMigrationStatus.BUILDING,
                        LongTermMemoryMigrationStatus.CATCHING_UP,
                        LongTermMemoryMigrationStatus.VALIDATING,
                    ]
                ),
            )
            .values(
                migration_delta_high_watermark=LongTermMemoryStore.migration_delta_high_watermark + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        return expected_high_watermark + 1


class CRUDLongTermMemoryEmbeddingSelectionToken:
    async def get_by_digest(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
        token_digest: str,
    ) -> LongTermMemoryEmbeddingSelectionToken | None:
        result = await db.execute(
            select(LongTermMemoryEmbeddingSelectionToken)
            .where(
                LongTermMemoryEmbeddingSelectionToken.uid == uid,
                LongTermMemoryEmbeddingSelectionToken.profile_id == profile_id,
                LongTermMemoryEmbeddingSelectionToken.token_digest == token_digest,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
        token_digest: str,
        profile_config_digest: str,
        active_embedding_revision: int,
        target_embedding_channel_id: int,
        target_embedding_model_id: str,
        target_embedding_dimensions: int,
        target_embedding_signature: str,
        expires_at: datetime,
        commit: bool = True,
    ) -> LongTermMemoryEmbeddingSelectionToken:
        token = LongTermMemoryEmbeddingSelectionToken(
            uid=uid,
            profile_id=profile_id,
            token_digest=token_digest,
            profile_config_digest=profile_config_digest,
            active_embedding_revision=active_embedding_revision,
            target_embedding_channel_id=target_embedding_channel_id,
            target_embedding_model_id=target_embedding_model_id,
            target_embedding_dimensions=target_embedding_dimensions,
            target_embedding_signature=target_embedding_signature,
            expires_at=expires_at,
        )
        db.add(token)
        await _finish(db, commit=commit)
        await db.refresh(token)
        return token

    async def consume_if_available(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
        token_digest: str,
        consumed_at: datetime,
        commit: bool = True,
    ) -> LongTermMemoryEmbeddingSelectionToken | None:
        result = await db.execute(
            update(LongTermMemoryEmbeddingSelectionToken)
            .where(
                LongTermMemoryEmbeddingSelectionToken.uid == uid,
                LongTermMemoryEmbeddingSelectionToken.profile_id == profile_id,
                LongTermMemoryEmbeddingSelectionToken.token_digest == token_digest,
                LongTermMemoryEmbeddingSelectionToken.consumed_at.is_(None),
            )
            .values(consumed_at=consumed_at)
            .execution_options(synchronize_session=False)
        )
        if (result.rowcount or 0) != 1:
            return None
        await _finish(db, commit=commit)
        refreshed = await db.execute(
            select(LongTermMemoryEmbeddingSelectionToken)
            .where(
                LongTermMemoryEmbeddingSelectionToken.uid == uid,
                LongTermMemoryEmbeddingSelectionToken.profile_id == profile_id,
                LongTermMemoryEmbeddingSelectionToken.token_digest == token_digest,
            )
            .execution_options(populate_existing=True)
        )
        return refreshed.scalars().first()
