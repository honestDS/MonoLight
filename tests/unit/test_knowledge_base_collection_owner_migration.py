"""知识库 collection owner 迁移测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import Column, Integer, MetaData, String, Table, delete, event, insert, inspect, text, update
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models.knowledge_base import KnowledgeBaseCollectionOwner
from scripts import migration_20260822_add_knowledge_base_collection_owner as migration

_COLLECTION_FIELDS = (
    "collection_name",
    "active_collection_name",
    "target_collection_name",
    "old_collection_name",
)
_OWNER_COLUMNS = (
    "collection_name",
    "knowledge_base_id",
    "cleanup_attempt_count",
    "cleanup_error",
    "created_at",
    "updated_at",
)

_LEGACY_METADATA = MetaData()
_LEGACY_KNOWLEDGE_BASE = Table(
    "knowledge_base",
    _LEGACY_METADATA,
    Column("id", Integer, primary_key=True),
    *(Column(field, String(255), nullable=True) for field in _COLLECTION_FIELDS),
)


@pytest_asyncio.fixture()
async def legacy_database(tmp_path: Path) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    database_path = tmp_path / "knowledge-base-collection-owner.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(_create_legacy_schema)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, session_factory
    finally:
        await engine.dispose()


def _create_legacy_schema(sync_connection) -> None:
    _LEGACY_METADATA.create_all(sync_connection, tables=[_LEGACY_KNOWLEDGE_BASE])


async def _seed_knowledge_bases(session_factory: async_sessionmaker[AsyncSession], rows: list[dict[str, object]]) -> None:
    async with session_factory() as session:
        await session.execute(insert(_LEGACY_KNOWLEDGE_BASE), rows)
        await session.commit()


async def _run_migration(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        await migration.migrate(session)


async def _update_collection(session_factory: async_sessionmaker[AsyncSession], knowledge_base_id: int, field: str, value: str) -> None:
    async with session_factory() as session:
        await session.execute(update(_LEGACY_KNOWLEDGE_BASE).where(_LEGACY_KNOWLEDGE_BASE.c.id == knowledge_base_id).values({field: value}))
        await session.commit()


async def _snapshot(engine: AsyncEngine) -> dict[str, object]:
    async with engine.connect() as connection:
        return await connection.run_sync(_snapshot_sync)


def _snapshot_sync(connection) -> dict[str, object]:
    inspector = inspect(connection)
    owner_rows = tuple(tuple(row[column] for column in _OWNER_COLUMNS) for row in connection.execute(text("SELECT collection_name, knowledge_base_id, cleanup_attempt_count, cleanup_error, created_at, updated_at FROM knowledge_base_collection_owner ORDER BY collection_name")).mappings())
    trigger_rows = tuple((row["name"], row["sql"]) for row in connection.execute(text("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'knowledge_base' ORDER BY name")).mappings())
    foreign_keys = tuple(
        sorted(
            (
                tuple(str(column) for column in record.get("constrained_columns") or ()),
                str(record.get("referred_table") or "").lower(),
                tuple(str(column) for column in record.get("referred_columns") or ()),
                str((record.get("options") or {}).get("ondelete") or "").upper() or None,
            )
            for record in inspector.get_foreign_keys("knowledge_base_collection_owner")
        )
    )
    indexes = tuple(
        sorted(
            (
                str(record.get("name")),
                tuple(str(column) for column in record.get("column_names") or ()),
                bool(record.get("unique")),
            )
            for record in inspector.get_indexes("knowledge_base_collection_owner")
        )
    )
    return {
        "owner_rows": owner_rows,
        "triggers": trigger_rows,
        "foreign_keys": foreign_keys,
        "indexes": indexes,
    }


async def _collection_row(engine: AsyncEngine, knowledge_base_id: int) -> dict[str, object]:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text("SELECT id, collection_name, active_collection_name, target_collection_name, old_collection_name FROM knowledge_base WHERE id = :knowledge_base_id"),
                    {"knowledge_base_id": knowledge_base_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


def _owner_mapping(snapshot: dict[str, object]) -> dict[str, int | None]:
    return {row[0]: row[1] for row in snapshot["owner_rows"]}


def _trigger_names(snapshot: dict[str, object]) -> set[str]:
    return {row[0] for row in snapshot["triggers"]}


@pytest.mark.asyncio
async def test_collection_owner_migration_backfills_history_and_is_idempotent(legacy_database) -> None:
    engine, session_factory = legacy_database
    row = {
        "id": 1,
        "collection_name": "collection-primary",
        "active_collection_name": "collection-primary",
        "target_collection_name": "collection-target",
        "old_collection_name": "collection-old",
    }
    await _seed_knowledge_bases(session_factory, [row])

    await _run_migration(session_factory)
    first_snapshot = await _snapshot(engine)

    expected_names = {str(row[field]) for field in _COLLECTION_FIELDS}
    assert set(_owner_mapping(first_snapshot)) == expected_names
    assert _owner_mapping(first_snapshot) == {
        "collection-primary": 1,
        "collection-target": 1,
        "collection-old": 1,
    }
    assert first_snapshot["foreign_keys"] == ((("knowledge_base_id",), "knowledge_base", ("id",), "SET NULL"),)
    assert _trigger_names(first_snapshot) == set(migration._OWNER_TRIGGER_NAMES)
    assert set(first_snapshot["indexes"]) == {
        (
            index.name,
            tuple(column.name for column in index.columns),
            bool(index.unique),
        )
        for index in KnowledgeBaseCollectionOwner.__table__.indexes
    }

    await _run_migration(session_factory)
    second_snapshot = await _snapshot(engine)
    assert second_snapshot == first_snapshot

    async with session_factory() as session:
        await session.execute(delete(_LEGACY_KNOWLEDGE_BASE).where(_LEGACY_KNOWLEDGE_BASE.c.id == 1))
        await session.commit()
    deleted_snapshot = await _snapshot(engine)
    assert {row[1] for row in deleted_snapshot["owner_rows"]} == {None}


@pytest.mark.asyncio
async def test_collection_owner_migration_retries_after_historical_conflict(legacy_database) -> None:
    engine, session_factory = legacy_database
    await _seed_knowledge_bases(
        session_factory,
        [
            {
                "id": 1,
                "collection_name": "kb-one-collection",
                "active_collection_name": "cross-field-conflict",
                "target_collection_name": "kb-one-target",
                "old_collection_name": "kb-one-old",
            },
            {
                "id": 2,
                "collection_name": "kb-two-collection",
                "active_collection_name": "kb-two-active",
                "target_collection_name": "cross-field-conflict",
                "old_collection_name": "kb-two-old",
            },
        ],
    )

    with pytest.raises(RuntimeError):
        await _run_migration(session_factory)

    await _update_collection(session_factory, 2, "target_collection_name", "kb-two-target")
    await _run_migration(session_factory)
    snapshot = await _snapshot(engine)

    assert _owner_mapping(snapshot) == {
        "kb-one-collection": 1,
        "cross-field-conflict": 1,
        "kb-one-target": 1,
        "kb-one-old": 1,
        "kb-two-collection": 2,
        "kb-two-active": 2,
        "kb-two-target": 2,
        "kb-two-old": 2,
    }
    assert _trigger_names(snapshot) == set(migration._OWNER_TRIGGER_NAMES)


@pytest.mark.asyncio
async def test_collection_owner_triggers_reject_all_cross_field_conflicts(legacy_database) -> None:
    engine, session_factory = legacy_database
    first_row = {
        "id": 1,
        "collection_name": "kb-one-collection",
        "active_collection_name": "kb-one-active",
        "target_collection_name": "kb-one-target",
        "old_collection_name": "kb-one-old",
    }
    second_row = {
        "id": 2,
        "collection_name": "kb-two-collection",
        "active_collection_name": "kb-two-active",
        "target_collection_name": "kb-two-target",
        "old_collection_name": "kb-two-old",
    }
    await _seed_knowledge_bases(session_factory, [first_row, second_row])
    await _run_migration(session_factory)

    for source_field in _COLLECTION_FIELDS:
        for target_field in _COLLECTION_FIELDS:
            async with session_factory() as session:
                with pytest.raises(IntegrityError):
                    async with session.begin():
                        await session.execute(update(_LEGACY_KNOWLEDGE_BASE).where(_LEGACY_KNOWLEDGE_BASE.c.id == 2).values({target_field: first_row[source_field]}))

    assert await _collection_row(engine, 2) == second_row


@pytest.mark.asyncio
async def test_collection_owner_migration_recovers_partial_owner_backfill(legacy_database) -> None:
    engine, session_factory = legacy_database
    row = {
        "id": 1,
        "collection_name": "partial-collection",
        "active_collection_name": "partial-active",
        "target_collection_name": "partial-target",
        "old_collection_name": "partial-old",
    }
    await _seed_knowledge_bases(session_factory, [row])

    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: KnowledgeBaseCollectionOwner.__table__.create(sync_connection, checkfirst=False))
        await connection.execute(
            text("INSERT INTO knowledge_base_collection_owner (collection_name, knowledge_base_id, cleanup_attempt_count, cleanup_error, created_at, updated_at) VALUES (:collection_name, :knowledge_base_id, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
            {"collection_name": row["collection_name"], "knowledge_base_id": row["id"]},
        )

    partial_snapshot = await _snapshot(engine)
    assert len(partial_snapshot["owner_rows"]) == 1
    assert partial_snapshot["triggers"] == ()

    await _run_migration(session_factory)
    first_snapshot = await _snapshot(engine)
    assert _owner_mapping(first_snapshot) == {
        "partial-collection": 1,
        "partial-active": 1,
        "partial-target": 1,
        "partial-old": 1,
    }
    assert _trigger_names(first_snapshot) == set(migration._OWNER_TRIGGER_NAMES)

    await _run_migration(session_factory)
    assert await _snapshot(engine) == first_snapshot


def _normalized_sql(statement: object) -> str:
    return " ".join(str(statement).split()).upper().replace("`", "")


def test_collection_owner_mysql_ddl_and_triggers_compile() -> None:
    dialect = mysql.dialect()
    owner_table = KnowledgeBaseCollectionOwner.__table__
    table_sql = _normalized_sql(CreateTable(owner_table).compile(dialect=dialect))

    assert "CREATE TABLE" in table_sql
    assert "KNOWLEDGE_BASE_COLLECTION_OWNER" in table_sql
    assert "FOREIGN KEY" in table_sql
    assert "KNOWLEDGE_BASE_ID" in table_sql
    assert "REFERENCES KNOWLEDGE_BASE (ID)" in table_sql
    assert "ON DELETE SET NULL" in table_sql

    for index in owner_table.indexes:
        index_sql = _normalized_sql(CreateIndex(index).compile(dialect=dialect))
        assert "CREATE INDEX" in index_sql
        assert index.name.upper() in index_sql
        assert tuple(column.name.upper() for column in index.columns) == ("KNOWLEDGE_BASE_ID",)

    connection = SimpleNamespace(dialect=dialect)
    trigger_statements = migration._mysql_trigger_statements(connection)
    compiled_triggers = [_normalized_sql(text(statement).compile(dialect=dialect)) for statement in trigger_statements]
    assert len(compiled_triggers) == 4
    assert [name.upper() in statement for name, statement in zip(migration._OWNER_TRIGGER_NAMES, compiled_triggers, strict=True)] == [
        True,
        True,
        True,
        True,
    ]

    combined_triggers = "\n".join(compiled_triggers)
    assert "SIGNAL SQLSTATE '45000'" in combined_triggers
    assert combined_triggers.count("INSERT IGNORE") == len(_COLLECTION_FIELDS) * 2
    after_trigger_sql = (
        next(statement for statement in compiled_triggers if "AFTER INSERT ON KNOWLEDGE_BASE" in statement),
        next(statement for statement in compiled_triggers if "AFTER UPDATE ON KNOWLEDGE_BASE" in statement),
    )
    for statement in after_trigger_sql:
        registration_blocks = statement.split("INSERT IGNORE")[1:]
        assert len(registration_blocks) == len(_COLLECTION_FIELDS)
        for field, registration_block in zip(_COLLECTION_FIELDS, registration_blocks, strict=True):
            collection_marker = f"COLLECTION_NAME = NEW.{field.upper()}"
            owner_id_marker = "KNOWLEDGE_BASE_ID = NEW.ID"
            signal_marker = "SIGNAL SQLSTATE '45000'"
            assert "IF NOT EXISTS" in registration_block
            assert "FROM KNOWLEDGE_BASE_COLLECTION_OWNER" in registration_block
            assert collection_marker in registration_block
            assert owner_id_marker in registration_block
            assert signal_marker in registration_block
            assert registration_block.index("IF NOT EXISTS") < registration_block.index("FROM KNOWLEDGE_BASE_COLLECTION_OWNER")
            assert registration_block.index("FROM KNOWLEDGE_BASE_COLLECTION_OWNER") < registration_block.index(collection_marker)
            assert registration_block.index(collection_marker) < registration_block.index(owner_id_marker)
            assert registration_block.index(owner_id_marker) < registration_block.index(signal_marker)
    for field in _COLLECTION_FIELDS:
        assert field.upper() in combined_triggers
    assert ("UPDATE KNOWLEDGE_BASE_COLLECTION_OWNER SET KNOWLEDGE_BASE_ID = NULL, CLEANUP_ATTEMPT_COUNT = 0, CLEANUP_ERROR = NULL, UPDATED_AT = CURRENT_TIMESTAMP WHERE KNOWLEDGE_BASE_ID = NEW.ID") in combined_triggers
