from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scripts import migration_20260813_add_memory_id_allocator as migration


@pytest_asyncio.fixture
async def legacy_database() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE long_term_memory_record ("
                "id INTEGER PRIMARY KEY, uid VARCHAR(100) NOT NULL, memory_key VARCHAR(255), content TEXT NOT NULL, "
                "content_hash VARCHAR(64), version INTEGER NOT NULL, indexed_version INTEGER NOT NULL, vector_item_id VARCHAR(255), "
                "is_active BOOLEAN NOT NULL, pending_mutation_job_id INTEGER, index_status VARCHAR(20) NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
                "CONSTRAINT uq_legacy_memory_uid_key UNIQUE (uid, memory_key), "
                "CONSTRAINT uq_legacy_memory_uid_hash UNIQUE (uid, content_hash), "
                "CONSTRAINT uq_legacy_memory_vector UNIQUE (vector_item_id)"
                ")"
            )
        )
        await connection.execute(text("CREATE INDEX ix_legacy_memory_uid ON long_term_memory_record (uid)"))
        for table in ("long_term_memory_revision", "long_term_memory_mutation_job", "long_term_memory_embedding_delta"):
            await connection.execute(text(f"CREATE TABLE {table} (memory_id INTEGER)"))
        await connection.execute(
            text(
                "INSERT INTO long_term_memory_record "
                "(id, uid, memory_key, content, content_hash, version, indexed_version, vector_item_id, is_active, index_status, created_at, updated_at) "
                "VALUES (2, 'legacy-user', 'legacy-key', 'legacy content', 'legacy-hash', 1, 1, 'legacy-vector', 1, 'ready', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        await connection.execute(text("INSERT INTO long_term_memory_revision (memory_id) VALUES (500)"))
        await connection.execute(text("INSERT INTO long_term_memory_mutation_job (memory_id) VALUES (90)"))
        await connection.execute(text("INSERT INTO long_term_memory_embedding_delta (memory_id) VALUES (120)"))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_allocator_migration_rebuilds_legacy_sqlite_table_and_preserves_high_watermark(
    legacy_database: async_sessionmaker[AsyncSession],
) -> None:
    async with legacy_database() as session:
        await migration.migrate(session)
        await session.commit()
        await migration.migrate(session)
        await session.commit()

    async with legacy_database() as session:
        legacy = (await session.execute(text("SELECT uid, content FROM long_term_memory_record WHERE id = 2"))).one()
        await session.execute(
            text("INSERT INTO long_term_memory_record (uid, memory_key, content, content_hash, version, indexed_version, vector_item_id, is_active, index_status, created_at, updated_at) VALUES ('new-user', 'new-key', 'new content', 'new-hash', 0, 0, 'new-vector', 0, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        )
        new_id = (await session.execute(text("SELECT id FROM long_term_memory_record WHERE uid = 'new-user'"))).scalar_one()
        await session.execute(text("DELETE FROM long_term_memory_record WHERE id = :id"), {"id": new_id})
        await session.execute(
            text(
                "INSERT INTO long_term_memory_record "
                "(uid, memory_key, content, content_hash, version, indexed_version, vector_item_id, is_active, index_status, created_at, updated_at) "
                "VALUES ('new-user-2', 'new-key-2', 'new content 2', 'new-hash-2', 0, 0, 'new-vector-2', 0, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        next_id_after_delete = (await session.execute(text("SELECT id FROM long_term_memory_record WHERE uid = 'new-user-2'"))).scalar_one()
        await session.commit()

    assert legacy == ("legacy-user", "legacy content")
    assert new_id == 501
    assert next_id_after_delete == 502

    async with legacy_database() as session:
        table_sql = (await session.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'long_term_memory_record'"))).scalar_one()
        connection = await session.connection()
        indexes = await connection.run_sync(lambda sync_connection: {index["name"] for index in inspect(sync_connection).get_indexes("long_term_memory_record")})
    assert "AUTOINCREMENT" in table_sql.upper()
    assert "ix_legacy_memory_uid" in indexes


@pytest.mark.parametrize("dialect", [sqlite.dialect(), mysql.dialect(), postgresql.dialect()])
def test_allocator_migration_exposes_cross_database_ddl_and_sequence_helpers(dialect):
    assert callable(migration._sequence_updater(dialect.name))
