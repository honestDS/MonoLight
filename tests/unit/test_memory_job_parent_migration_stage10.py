from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, inspect, select
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.memory import LongTermMemoryMutationJob, LongTermMemoryMutationOperation
from scripts import migration_20260803_add_longterm_memory as legacy_migration
from scripts import migration_20260811_add_memory_job_parent as parent_migration


@pytest_asyncio.fixture
async def legacy_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        await legacy_migration.migrate(session)
        await session.commit()
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _job_schema(connection: Any) -> dict[str, set[str]]:
    inspector = inspect(connection)
    return {
        "columns": {column["name"] for column in inspector.get_columns("long_term_memory_mutation_job")},
        "indexes": {index["name"] for index in inspector.get_indexes("long_term_memory_mutation_job")},
    }


async def _insert_legacy_job(session: AsyncSession) -> int:
    result = await session.execute(
        legacy_migration.long_term_memory_mutation_job.insert().values(
            uid="legacy-owner",
            operation="EXTRACT",
            dedupe_key="legacy-job",
            status="PENDING",
            payload={"source": "legacy"},
        )
    )
    return int(result.inserted_primary_key[0])


@pytest.mark.parametrize("dialect", [sqlite.dialect(), mysql.dialect()])
def test_memory_job_parent_ddl_compiles_for_sqlite_and_mysql(dialect: Any) -> None:
    column = Column(parent_migration._PARENT_COLUMN, Integer, nullable=True)
    add_column_ddl = parent_migration._compile_add_column_ddl(
        parent_migration._JOB_TABLE,
        column,
        dialect,
    )
    index_ddl = parent_migration._compile_index_ddl(
        parent_migration._JOB_TABLE,
        parent_migration._PARENT_INDEX,
        (parent_migration._PARENT_COLUMN,),
        dialect,
    )

    assert "ADD COLUMN" in add_column_ddl
    assert "CREATE INDEX" in index_ddl


@pytest.mark.asyncio
async def test_memory_job_parent_migration_is_idempotent_and_preserves_jobs(
    legacy_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with legacy_session_factory() as session:
        legacy_job_id = await _insert_legacy_job(session)
        await session.commit()

        await parent_migration.migrate(session)
        await session.commit()
        await parent_migration.migrate(session)
        await session.commit()

        connection = await session.connection()
        schema = await connection.run_sync(_job_schema)
        assert parent_migration._PARENT_COLUMN in schema["columns"]
        assert parent_migration._PARENT_INDEX in schema["indexes"]

        legacy_job = await session.get(LongTermMemoryMutationJob, legacy_job_id)
        assert legacy_job is not None
        assert legacy_job.uid == "legacy-owner"
        assert legacy_job.dedupe_key == "legacy-job"
        assert legacy_job.payload == {"source": "legacy"}
        assert legacy_job.parent_job_id is None

        child_job = LongTermMemoryMutationJob(
            uid="legacy-owner",
            operation=LongTermMemoryMutationOperation.EXTRACT,
            dedupe_key="child-job",
            parent_job_id=legacy_job_id,
            payload={"source": "child"},
        )
        session.add(child_job)
        await session.commit()

        jobs = list((await session.execute(select(LongTermMemoryMutationJob).order_by(LongTermMemoryMutationJob.id))).scalars())
        assert len(jobs) == 2
        assert jobs[1].parent_job_id == legacy_job_id
