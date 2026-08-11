from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from sqlalchemy import inspect, select
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from scripts import migration_20260803_add_longterm_memory as legacy_migration
from scripts import migration_20260805_add_memory_v2_organization as organization_migration
from scripts import migration_20260811_add_memory_job_parent as parent_migration


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: Any) -> None:
        pass


with patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient):
    from app.core.constants import (
        MEMORY_CONTENT_MAX_TOKENS,
        MEMORY_MAX_ACTIVE_RECORDS,
        MEMORY_ORGANIZE_POLICY_VERSION,
        MEMORY_ORGANIZE_TRIGGER_RECORDS,
    )
    from app.core.crud.memory import memory_store_crud
    from app.core.crud.memory_job import memory_job_crud
    from app.core.memory.management import list_jobs, list_memory_history
    from app.core.memory_jobs.handlers import create_default_memory_job_executor
    from app.core.utils.tokenizer import estimate_tokens
    from app.models.memory import (
        LongTermMemoryCapacityStatus,
        LongTermMemoryIndexStatus,
        LongTermMemoryMutationOperation,
        LongTermMemoryMutationStatus,
        LongTermMemoryOldCollectionCleanupStatus,
        LongTermMemoryRecord,
        LongTermMemoryRevision,
        LongTermMemorySource,
        LongTermMemoryStore,
    )


pytest_plugins = ("tests.unit.memory_stage5_fixture",)


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


async def _insert_store(session: AsyncSession, uid: str) -> None:
    await session.execute(
        legacy_migration.long_term_memory_store.insert().values(
            uid=uid,
            max_active_records=500,
            old_collection_cleanup_status="NONE",
            index_status="PENDING",
        )
    )


async def _insert_record(
    session: AsyncSession,
    *,
    uid: str,
    memory_key: str,
    content: str,
    content_hash: str,
    vector_item_id: str,
    source: str = "USER_API",
    is_active: bool = True,
    version: int = 1,
    deleted_at: Any = None,
) -> int:
    result = await session.execute(
        legacy_migration.long_term_memory_record.insert().values(
            uid=uid,
            memory_key=memory_key,
            memory_type="FACT",
            content=content,
            content_hash=content_hash,
            version=version,
            indexed_version=version,
            vector_item_id=vector_item_id,
            source=source,
            is_active=is_active,
            index_status="READY",
            deleted_at=deleted_at,
        )
    )
    return int(result.inserted_primary_key[0])


async def _insert_revision(
    session: AsyncSession,
    *,
    uid: str,
    memory_id: int,
    version: int,
    content: str,
    content_hash: str,
    source: str = "USER_API",
) -> int:
    result = await session.execute(
        legacy_migration.long_term_memory_revision.insert().values(
            uid=uid,
            memory_id=memory_id,
            version=version,
            memory_key=f"revision-{memory_id}-{version}",
            memory_type="FACT",
            content=content,
            content_hash=content_hash,
            source=source,
        )
    )
    return int(result.inserted_primary_key[0])


async def _insert_extract_job(session: AsyncSession, *, uid: str, memory_id: int) -> int:
    result = await session.execute(
        legacy_migration.long_term_memory_mutation_job.insert().values(
            uid=uid,
            operation="EXTRACT",
            dedupe_key=f"legacy-extract-{memory_id}",
            status="PENDING",
            memory_id=memory_id,
            payload={"memory_id": memory_id},
        )
    )
    return int(result.inserted_primary_key[0])


def _schema_snapshot(connection: Any) -> dict[str, Any]:
    inspector = inspect(connection)
    return {
        "columns": {
            table_name: {column["name"] for column in inspector.get_columns(table_name)}
            for table_name in (
                "long_term_memory_store",
                "long_term_memory_record",
                "long_term_memory_revision",
            )
        },
        "indexes": {
            table_name: {index["name"] for index in inspector.get_indexes(table_name)}
            for table_name in (
                "long_term_memory_store",
                "long_term_memory_record",
                "long_term_memory_revision",
            )
        },
    }


@pytest.mark.parametrize("dialect", [sqlite.dialect(), mysql.dialect()])
def test_memory_v2_organization_ddl_compiles_for_sqlite_and_mysql(dialect: Any) -> None:
    for table_name, columns in organization_migration._memory_v2_column_definitions():
        for column in columns:
            ddl = organization_migration._compile_add_column_ddl(table_name, column, dialect)
            assert ddl.strip()
            assert "ADD COLUMN" in ddl

    for table_name, index_name, column_names in organization_migration._MEMORY_V2_INDEX_DEFINITIONS:
        ddl = organization_migration._compile_index_ddl(table_name, index_name, column_names, dialect)
        assert ddl.strip()
        assert "CREATE INDEX" in ddl

    for table in (LongTermMemoryStore.__table__, LongTermMemoryRecord.__table__, LongTermMemoryRevision.__table__):
        assert str(CreateTable(table).compile(dialect=dialect)).strip()
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect)).strip()


@pytest.mark.asyncio
async def test_memory_v2_organization_migration_is_idempotent_and_preserves_legacy_data(
    legacy_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    short_content = "short legacy memory"
    long_content = "long-token " * 200
    snapshots: dict[int, tuple[str, int, str, str | None]] = {}

    async with legacy_session_factory() as session:
        await _insert_store(session, "normal-owner")
        await _insert_store(session, "count-over-limit-owner")
        await _insert_store(session, "token-over-limit-owner")
        await _insert_store(session, "legacy-history-owner")

        normal_id = await _insert_record(
            session,
            uid="normal-owner",
            memory_key="normal",
            content=short_content,
            content_hash="normal-hash",
            vector_item_id="normal-vector",
        )
        inactive_id = await _insert_record(
            session,
            uid="normal-owner",
            memory_key="inactive",
            content=long_content,
            content_hash="inactive-hash",
            vector_item_id="inactive-vector",
            is_active=False,
        )
        normal_revision_id = await _insert_revision(
            session,
            uid="normal-owner",
            memory_id=normal_id,
            version=1,
            content=short_content,
            content_hash="normal-hash",
        )
        for index in range(MEMORY_MAX_ACTIVE_RECORDS + 1):
            record_id = await _insert_record(
                session,
                uid="count-over-limit-owner",
                memory_key=f"count-{index}",
                content=f"counted memory {index}",
                content_hash=f"count-hash-{index}",
                vector_item_id=f"count-vector-{index}",
            )
            snapshots[record_id] = (f"counted memory {index}", 1, f"count-hash-{index}", f"count-vector-{index}")

        long_id = await _insert_record(
            session,
            uid="token-over-limit-owner",
            memory_key="long",
            content=long_content,
            content_hash="long-hash",
            vector_item_id="long-vector",
        )
        long_revision_id = await _insert_revision(
            session,
            uid="token-over-limit-owner",
            memory_id=long_id,
            version=1,
            content=long_content,
            content_hash="long-hash",
        )
        history_id = await _insert_record(
            session,
            uid="legacy-history-owner",
            memory_key="legacy-extract-memory",
            content="legacy extracted memory",
            content_hash="legacy-extract-hash",
            vector_item_id="legacy-extract-vector",
            source="AUTO_EXTRACT",
        )
        history_revision_id = await _insert_revision(
            session,
            uid="legacy-history-owner",
            memory_id=history_id,
            version=1,
            content="legacy extracted revision",
            content_hash="legacy-extract-revision-hash",
            source="AUTO_EXTRACT",
        )
        extract_job_id = await _insert_extract_job(session, uid="legacy-history-owner", memory_id=history_id)
        snapshots[normal_id] = (short_content, 1, "normal-hash", "normal-vector")
        snapshots[inactive_id] = (long_content, 1, "inactive-hash", "inactive-vector")
        snapshots[long_id] = (long_content, 1, "long-hash", "long-vector")
        snapshots[history_id] = ("legacy extracted memory", 1, "legacy-extract-hash", "legacy-extract-vector")
        await session.commit()

        await organization_migration.migrate(session)
        await session.commit()
        await organization_migration.migrate(session)
        await session.commit()
        await parent_migration.migrate(session)
        await session.commit()
        connection = await session.connection()
        schema = await connection.run_sync(_schema_snapshot)

        records = list((await session.execute(select(LongTermMemoryRecord).order_by(LongTermMemoryRecord.id))).scalars().all())
        revisions = list((await session.execute(select(LongTermMemoryRevision).order_by(LongTermMemoryRevision.id))).scalars().all())
        stores = list((await session.execute(select(LongTermMemoryStore).order_by(LongTermMemoryStore.uid))).scalars().all())

        assert {
            "organize_trigger_records",
            "auto_organize_enabled",
            "organization_channel_id",
            "organization_model_id",
            "organization_policy_version",
            "organization_last_job_id",
            "organization_last_run_at",
            "organization_error",
            "capacity_status",
        } <= schema["columns"]["long_term_memory_store"]
        assert {"content_token_count", "pinned", "last_recalled_at"} <= schema["columns"]["long_term_memory_record"]
        assert {"content_token_count"} <= schema["columns"]["long_term_memory_revision"]
        assert {
            "ix_long_term_memory_store_organization_channel_id",
            "ix_long_term_memory_store_organization_model_id",
            "ix_long_term_memory_store_organization_last_job_id",
            "ix_long_term_memory_store_capacity_status",
        } <= schema["indexes"]["long_term_memory_store"]
        assert "ix_ltm_record_eviction_candidate" in schema["indexes"]["long_term_memory_record"]

        normal_store = next(store for store in stores if store.uid == "normal-owner")
        count_store = next(store for store in stores if store.uid == "count-over-limit-owner")
        token_store = next(store for store in stores if store.uid == "token-over-limit-owner")
        history_store = next(store for store in stores if store.uid == "legacy-history-owner")

        assert normal_store.max_active_records == MEMORY_MAX_ACTIVE_RECORDS
        assert normal_store.organize_trigger_records == MEMORY_ORGANIZE_TRIGGER_RECORDS
        assert normal_store.auto_organize_enabled is False
        assert normal_store.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.NONE
        assert normal_store.index_status == LongTermMemoryIndexStatus.PENDING
        assert normal_store.organization_channel_id is None
        assert normal_store.organization_model_id is None
        assert normal_store.organization_policy_version == MEMORY_ORGANIZE_POLICY_VERSION
        assert normal_store.organization_last_job_id is None
        assert normal_store.organization_last_run_at is None
        assert normal_store.organization_error is None
        assert normal_store.capacity_status == LongTermMemoryCapacityStatus.NORMAL
        assert count_store.capacity_status == LongTermMemoryCapacityStatus.OVER_LIMIT
        assert token_store.capacity_status == LongTermMemoryCapacityStatus.OVER_LIMIT
        assert history_store.capacity_status == LongTermMemoryCapacityStatus.NORMAL

        record_by_id = {record.id: record for record in records}
        assert len(records) == MEMORY_MAX_ACTIVE_RECORDS + 5
        assert len(record_by_id) == len(records)
        assert record_by_id[normal_id].content_token_count == estimate_tokens(short_content)
        assert record_by_id[normal_id].pinned is False
        assert record_by_id[normal_id].last_recalled_at is None
        assert record_by_id[inactive_id].content_token_count == 0
        assert record_by_id[long_id].content_token_count == estimate_tokens(long_content)
        assert record_by_id[history_id].source == LongTermMemorySource.AUTO_EXTRACT
        assert record_by_id[long_id].content == long_content
        assert len(revisions) == 3
        assert {revision.id for revision in revisions} == {
            normal_revision_id,
            long_revision_id,
            history_revision_id,
        }
        revision_by_id = {revision.id: revision for revision in revisions}
        assert revision_by_id[normal_revision_id].content_token_count == estimate_tokens(short_content)
        assert revision_by_id[long_revision_id].content_token_count == estimate_tokens(long_content)
        assert revision_by_id[history_revision_id].content_token_count == estimate_tokens("legacy extracted revision")
        assert (
            revision_by_id[normal_revision_id].content,
            revision_by_id[normal_revision_id].version,
            revision_by_id[normal_revision_id].content_hash,
        ) == (short_content, 1, "normal-hash")
        assert (
            revision_by_id[long_revision_id].content,
            revision_by_id[long_revision_id].version,
            revision_by_id[long_revision_id].content_hash,
        ) == (long_content, 1, "long-hash")
        assert (
            revision_by_id[history_revision_id].content,
            revision_by_id[history_revision_id].version,
            revision_by_id[history_revision_id].content_hash,
        ) == ("legacy extracted revision", 1, "legacy-extract-revision-hash")

        for record in records:
            expected = snapshots[record.id]
            assert (record.content, record.version, record.content_hash, record.vector_item_id) == expected
        assert all(record.content_token_count <= MEMORY_CONTENT_MAX_TOKENS or record.id == long_id for record in records)

        new_store = await memory_store_crud.create(session, uid="new-orm-store", commit=False)
        await session.commit()
        assert new_store.max_active_records == MEMORY_MAX_ACTIVE_RECORDS
        assert new_store.organize_trigger_records == MEMORY_ORGANIZE_TRIGGER_RECORDS

        history = await list_memory_history(session, uid="legacy-history-owner", memory_id=history_id)
        jobs = await list_jobs(session, uid="legacy-history-owner")
        executor = create_default_memory_job_executor(session_factory=legacy_session_factory)
        claimable_jobs = await memory_job_crud.list_claimable_for_worker(
            session,
            enabled_operations=executor.enabled_operations,
        )

    assert history["total"] == 1
    assert history["items"][0]["source"] == LongTermMemorySource.AUTO_EXTRACT.value
    assert jobs["total"] == 1
    assert jobs["items"][0]["id"] == extract_job_id
    assert jobs["items"][0]["operation"] == LongTermMemoryMutationOperation.EXTRACT.value
    assert jobs["items"][0]["status"] == LongTermMemoryMutationStatus.PENDING.value
    assert LongTermMemoryMutationOperation.EXTRACT not in executor.enabled_operations
    assert all(job.id != extract_job_id for job in claimable_jobs)
