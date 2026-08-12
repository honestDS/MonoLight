from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core.constants import ERR_MEMORY_RESTORE_JOB_DISABLED
from app.core.i18n import t
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
)
from scripts import migration_20260812_disable_memory_restore as restore_migration

MEMORY_TABLES = (
    LongTermMemoryRecord.__table__,
    LongTermMemoryMutationJob.__table__,
)


@pytest_asyncio.fixture
async def memory_session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory-restore-disable.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=MEMORY_TABLES,
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restore_disable_migration_fails_unfinished_jobs_releases_records_and_preserves_history(
    memory_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original_finished_at = datetime(2026, 8, 12, 20, 0)
    async with memory_session_factory() as session:
        records = [
            LongTermMemoryRecord(
                uid="restore-migration-user",
                content="placeholder",
            )
            for _ in range(3)
        ]
        session.add_all(records)
        await session.flush()

        unfinished_jobs = [
            LongTermMemoryMutationJob(
                uid="restore-migration-user",
                operation=LongTermMemoryMutationOperation.RESTORE,
                dedupe_key=f"unfinished-{status.value}",
                active_mutation_key=f"restore-target-{status.value}",
                status=status,
                memory_id=record.id,
                expected_version=1,
                payload={"restored_from_version": 1},
                locked_by=f"worker-{status.value}",
                lock_until=original_finished_at,
            )
            for record, status in zip(
                records,
                (
                    LongTermMemoryMutationStatus.PENDING,
                    LongTermMemoryMutationStatus.RUNNING,
                    LongTermMemoryMutationStatus.RETRY,
                ),
                strict=True,
            )
        ]
        succeeded = LongTermMemoryMutationJob(
            uid="restore-migration-user",
            operation=LongTermMemoryMutationOperation.RESTORE,
            dedupe_key="succeeded-history",
            status=LongTermMemoryMutationStatus.SUCCEEDED,
            memory_id=10,
            payload={"restored_from_version": 1},
            error="historical success detail",
            finished_at=original_finished_at,
        )
        failed = LongTermMemoryMutationJob(
            uid="restore-migration-user",
            operation=LongTermMemoryMutationOperation.RESTORE,
            dedupe_key="failed-history",
            status=LongTermMemoryMutationStatus.FAILED,
            memory_id=11,
            payload={"restored_from_version": 2},
            error="historical failure detail",
            finished_at=original_finished_at,
        )
        session.add_all([*unfinished_jobs, succeeded, failed])
        await session.flush()
        other_uid_record = LongTermMemoryRecord(
            uid="restore-migration-other-user",
            content="must remain reserved",
            pending_mutation_job_id=unfinished_jobs[0].id,
        )
        session.add(other_uid_record)
        await session.flush()
        for record, job in zip(records, unfinished_jobs, strict=True):
            record.pending_mutation_job_id = job.id
        await session.commit()

        await restore_migration.migrate(session)
        await session.commit()
        await restore_migration.migrate(session)
        await session.commit()

        persisted_jobs = list((await session.execute(select(LongTermMemoryMutationJob).order_by(LongTermMemoryMutationJob.id))).scalars())
        persisted_records = [await session.get(LongTermMemoryRecord, record.id) for record in records]
        persisted_other_uid_record = await session.get(LongTermMemoryRecord, other_uid_record.id)

    disabled_error = t(ERR_MEMORY_RESTORE_JOB_DISABLED)
    jobs_by_key = {job.dedupe_key: job for job in persisted_jobs}
    for status in ("pending", "running", "retry"):
        job = jobs_by_key[f"unfinished-{status}"]
        assert job.status == LongTermMemoryMutationStatus.FAILED
        assert job.error == disabled_error
        assert job.finished_at is not None
        assert job.updated_at is not None
        assert job.locked_by is None
        assert job.lock_until is None
        assert job.active_mutation_key is None
    assert [record.pending_mutation_job_id for record in persisted_records if record is not None] == [None, None, None]
    assert persisted_other_uid_record is not None
    assert persisted_other_uid_record.pending_mutation_job_id == jobs_by_key["unfinished-pending"].id
    assert jobs_by_key["succeeded-history"].status == LongTermMemoryMutationStatus.SUCCEEDED
    assert jobs_by_key["succeeded-history"].error == "historical success detail"
    assert jobs_by_key["succeeded-history"].finished_at == original_finished_at
    assert jobs_by_key["failed-history"].status == LongTermMemoryMutationStatus.FAILED
    assert jobs_by_key["failed-history"].error == "historical failure detail"
    assert jobs_by_key["failed-history"].finished_at == original_finished_at
