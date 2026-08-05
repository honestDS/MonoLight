from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core.memory import build_memory_content_hash
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryRevision,
    LongTermMemorySource,
    LongTermMemoryType,
)
from scripts import migration_20260805_hard_delete_memory_tombstones as hard_delete_migration

MEMORY_TABLES = (
    LongTermMemoryRecord.__table__,
    LongTermMemoryRevision.__table__,
    LongTermMemoryMutationJob.__table__,
)


def _snapshot_source_payload(version: int) -> dict[str, object]:
    return {
        "version": version,
        "source": LongTermMemorySource.USER_API.value,
        "source_id": None,
        "source_session_id": None,
        "source_profile_id": None,
        "source_message_id": None,
    }


@pytest.mark.asyncio
async def test_hard_delete_migration_backfills_snapshots_and_keeps_unrecoverable_tombstones(
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory-hard-delete-migration.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    recoverable_id = 7
    unrecoverable_id = 8
    content = "historical memory content"
    content_hash = build_memory_content_hash(content)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: SQLModel.metadata.create_all(
                    sync_connection,
                    tables=MEMORY_TABLES,
                )
            )

        async with session_factory() as session:
            session.add_all(
                [
                    LongTermMemoryRecord(
                        id=recoverable_id,
                        uid="migration-user",
                        memory_key=None,
                        content="",
                        content_hash=None,
                        version=2,
                        indexed_version=0,
                        vector_item_id=None,
                        is_active=False,
                        pending_mutation_job_id=None,
                        index_status=LongTermMemoryRecordIndexStatus.READY,
                        deleted_at=now,
                    ),
                    LongTermMemoryRecord(
                        id=unrecoverable_id,
                        uid="migration-user",
                        memory_key=None,
                        content="",
                        content_hash=None,
                        version=1,
                        indexed_version=0,
                        vector_item_id=None,
                        is_active=False,
                        pending_mutation_job_id=None,
                        index_status=LongTermMemoryRecordIndexStatus.READY,
                        deleted_at=now,
                    ),
                    LongTermMemoryRevision(
                        uid="migration-user",
                        memory_id=recoverable_id,
                        version=2,
                        memory_key="historical-key",
                        memory_type=LongTermMemoryType.FACT,
                        importance=6,
                        scope="project",
                        content=content,
                        content_hash=content_hash,
                        source=LongTermMemorySource.USER_API,
                        change_evidence="historical evidence",
                    ),
                    LongTermMemoryMutationJob(
                        uid="migration-user",
                        operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                        dedupe_key="recoverable-delete",
                        status=LongTermMemoryMutationStatus.SUCCEEDED,
                        memory_id=recoverable_id,
                        expected_version=2,
                        payload=_snapshot_source_payload(2),
                        result={"memory_id": recoverable_id, "version": 2},
                    ),
                    LongTermMemoryMutationJob(
                        uid="migration-user",
                        operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                        dedupe_key="unrecoverable-delete",
                        status=LongTermMemoryMutationStatus.SUCCEEDED,
                        memory_id=unrecoverable_id,
                        expected_version=1,
                        payload=_snapshot_source_payload(1),
                        result={"memory_id": unrecoverable_id, "version": 1},
                    ),
                ]
            )
            await session.commit()

            await hard_delete_migration.migrate(session)
            await session.commit()
            await hard_delete_migration.migrate(session)
            await session.commit()

            recoverable_record = await session.get(LongTermMemoryRecord, recoverable_id)
            unrecoverable_record = await session.get(LongTermMemoryRecord, unrecoverable_id)
            jobs = list((await session.execute(select(LongTermMemoryMutationJob).order_by(LongTermMemoryMutationJob.id))).scalars().all())

        assert recoverable_record is None
        assert unrecoverable_record is not None
        snapshot = jobs[0].payload["record_snapshot"]
        assert snapshot["memory_key"] == "historical-key"
        assert snapshot["content"] == content
        assert snapshot["content_hash"] == content_hash
        assert snapshot["version"] == 2
        assert jobs[0].result["record_snapshot"] == snapshot
        assert "record_snapshot" not in jobs[1].payload
        assert "record_snapshot" not in jobs[1].result
    finally:
        await engine.dispose()
