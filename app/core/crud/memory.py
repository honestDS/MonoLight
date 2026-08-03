from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryEmbeddingSelectionToken,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryRevision,
    LongTermMemoryStore,
)
from app.providers.database.time import get_database_time


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
        result = await db.execute(
            update(LongTermMemoryStore)
            .where(
                LongTermMemoryStore.uid == uid,
                LongTermMemoryStore.active_embedding_revision == expected_active_revision,
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


class CRUDLongTermMemoryRecord:
    async def get_by_id(self, db: AsyncSession, *, uid: str, memory_id: int) -> LongTermMemoryRecord | None:
        result = await db.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.id == memory_id).execution_options(populate_existing=True))
        return result.scalars().first()

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
        result = await db.execute(
            select(LongTermMemoryRecord).where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id.in_(memory_ids),
                LongTermMemoryRecord.is_active.is_(True),
                LongTermMemoryRecord.deleted_at.is_(None),
                LongTermMemoryRecord.suppress_recall.is_(False),
                LongTermMemoryRecord.index_status == LongTermMemoryRecordIndexStatus.READY,
                LongTermMemoryRecord.indexed_version == LongTermMemoryRecord.version,
                LongTermMemoryRecord.vector_item_id.is_not(None),
                LongTermMemoryRecord.vector_item_id != "",
            )
        )
        return list(result.scalars().all())

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

    async def create_pending_placeholder(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        commit: bool = True,
    ) -> LongTermMemoryRecord:
        now = await get_database_time(db)
        record = LongTermMemoryRecord.model_validate(
            {
                "uid": uid,
                "memory_key": None,
                "content": "",
                "content_hash": None,
                "version": 0,
                "indexed_version": 0,
                "is_active": False,
                "pending_mutation_job_id": job_id,
                "index_status": LongTermMemoryRecordIndexStatus.PENDING,
                "created_at": now,
                "updated_at": now,
            }
        )
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
            "importance",
            "scope",
            "content",
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


memory_store_crud = CRUDLongTermMemoryStore()
memory_embedding_selection_token_crud = CRUDLongTermMemoryEmbeddingSelectionToken()
memory_record_crud = CRUDLongTermMemoryRecord()
memory_revision_crud = CRUDLongTermMemoryRevision()
memory_embedding_revision_crud = CRUDLongTermMemoryEmbeddingRevision()
memory_embedding_delta_crud = CRUDLongTermMemoryEmbeddingDelta()
memory_reference_crud = CRUDLongTermMemoryReference()

long_term_memory_store_crud = memory_store_crud
long_term_memory_embedding_selection_token_crud = memory_embedding_selection_token_crud
long_term_memory_record_crud = memory_record_crud
long_term_memory_revision_crud = memory_revision_crud
long_term_memory_embedding_revision_crud = memory_embedding_revision_crud
long_term_memory_embedding_delta_crud = memory_embedding_delta_crud
long_term_memory_reference_crud = memory_reference_crud
