from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from scripts import migration_20260913_add_knowledge_organization_state as organization_state_migration
from scripts import migration_20260913_make_managed_knowledge_unique_fields_nullable as managed_nullable_migration
from scripts import migration_20260914_restore_managed_knowledge_owner_fk as managed_owner_fk_migration

_LEGACY_SCHEMA_SQL = (
    """
    CREATE TABLE knowledge_base (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid TEXT NOT NULL,
        name TEXT NOT NULL,
        legacy_kb_marker TEXT NOT NULL,
        CONSTRAINT uq_knowledge_base_id_uid UNIQUE (id, uid)
    )
    """,
    """
    CREATE TABLE managed_knowledge_item (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        knowledge_base_id INTEGER NOT NULL,
        uid TEXT NOT NULL,
        knowledge_key TEXT NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        legacy_item_marker TEXT NOT NULL,
        CONSTRAINT uq_managed_knowledge_item_kb_key UNIQUE (knowledge_base_id, knowledge_key),
        CONSTRAINT uq_managed_knowledge_item_kb_content_hash UNIQUE (knowledge_base_id, content_hash),
        CONSTRAINT fk_managed_knowledge_item_kb_owner
            FOREIGN KEY (knowledge_base_id, uid)
            REFERENCES knowledge_base (id, uid)
            ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_managed_knowledge_item_legacy_content ON managed_knowledge_item (content)",
)


def _sqlite_foreign_key_signatures(connection: Any, table_name: str) -> tuple[tuple[Any, ...], ...]:
    quoted_table_name = connection.dialect.identifier_preparer.quote(table_name)
    rows = connection.exec_driver_sql(f"PRAGMA foreign_key_list({quoted_table_name})").mappings().all()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["id"]), []).append(dict(row))

    signatures = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda row: int(row["seq"]))
        on_delete = {str(row["on_delete"]).strip().upper() for row in ordered}
        if len(on_delete) != 1:
            raise AssertionError(f"inconsistent SQLite ON DELETE actions for {table_name}")
        signatures.append(
            (
                tuple(str(row["from"]) for row in ordered),
                str(ordered[0]["table"]),
                tuple(str(row["to"]) for row in ordered),
                next(iter(on_delete)),
            )
        )
    return tuple(sorted(signatures, key=repr))


def _table_snapshot(connection: Any, table_name: str) -> dict[str, Any]:
    inspector = inspect(connection)
    columns = tuple((str(column["name"]), bool(column.get("nullable"))) for column in inspector.get_columns(table_name))
    foreign_keys = _sqlite_foreign_key_signatures(connection, table_name)
    unique_constraints = tuple(
        sorted(
            (
                str(record.get("name")),
                tuple(record.get("column_names") or ()),
            )
            for record in inspector.get_unique_constraints(table_name)
        )
    )
    indexes = tuple(
        sorted(
            (
                str(record.get("name")),
                tuple(record.get("column_names") or ()),
                bool(record.get("unique")),
            )
            for record in inspector.get_indexes(table_name)
        )
    )
    table_sql = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
        {"table_name": table_name},
    ).scalar_one_or_none()
    return {
        "columns": columns,
        "foreign_keys": foreign_keys,
        "unique_constraints": unique_constraints,
        "indexes": indexes,
        "sql": str(table_sql or ""),
    }


async def _schema_snapshot(session: AsyncSession) -> dict[str, dict[str, Any]]:
    connection = await session.connection()
    return await connection.run_sync(
        lambda sync_connection: {
            "knowledge_base": _table_snapshot(sync_connection, "knowledge_base"),
            "managed_knowledge_item": _table_snapshot(sync_connection, "managed_knowledge_item"),
        }
    )


async def _foreign_keys_enabled(session: AsyncSession) -> bool:
    return bool((await session.execute(text("PRAGMA foreign_keys"))).scalar_one())


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "knowledge-organization-migration-knowledge_organization_migration.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    try:
        async with engine.begin() as connection:
            for statement in _LEGACY_SCHEMA_SQL:
                await connection.exec_driver_sql(statement)
            assert (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() == 1
            await connection.exec_driver_sql("INSERT INTO knowledge_base (uid, name, legacy_kb_marker) VALUES ('legacy-user', 'legacy-base', 'kb-legacy-marker')")
            await connection.exec_driver_sql("INSERT INTO managed_knowledge_item (knowledge_base_id, uid, knowledge_key, content, content_hash, legacy_item_marker) VALUES (1, 'legacy-user', 'legacy-key', 'legacy-content', 'legacy-hash', 'item-legacy-marker')")

        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_organization_migrations_preserve_legacy_sqlite_schema(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        before = await _schema_snapshot(session)
        before_managed_columns = dict(before["managed_knowledge_item"]["columns"])
        assert before_managed_columns["knowledge_key"] is False
        assert before_managed_columns["content_hash"] is False
        assert await _foreign_keys_enabled(session)

        original_knowledge_base = dict((await session.execute(text("SELECT id, uid, name, legacy_kb_marker FROM knowledge_base WHERE id = 1"))).mappings().one())
        original_item = dict((await session.execute(text("SELECT id, knowledge_base_id, uid, knowledge_key, content, content_hash, legacy_item_marker FROM managed_knowledge_item WHERE id = 1"))).mappings().one())
        await session.rollback()

        await organization_state_migration.migrate(session)
        await session.commit()
        await managed_nullable_migration.migrate(session)
        await session.commit()
        await managed_owner_fk_migration.migrate(session)
        await session.commit()
        first_schema = await _schema_snapshot(session)
        await session.rollback()

        await organization_state_migration.migrate(session)
        await session.commit()
        await managed_nullable_migration.migrate(session)
        await session.commit()
        await managed_owner_fk_migration.migrate(session)
        await session.commit()
        second_schema = await _schema_snapshot(session)
        await session.rollback()

        assert second_schema == first_schema

        knowledge_base_schema = first_schema["knowledge_base"]
        knowledge_base_columns = dict(knowledge_base_schema["columns"])
        assert {
            "organization_last_job_id",
            "organization_last_run_at",
            "organization_error",
        } <= knowledge_base_columns.keys()
        assert knowledge_base_columns["organization_last_job_id"] is True
        assert knowledge_base_columns["organization_last_run_at"] is True
        assert knowledge_base_columns["organization_error"] is True
        assert (
            "ix_knowledge_base_organization_last_job_id",
            ("organization_last_job_id",),
            False,
        ) in knowledge_base_schema["indexes"]

        managed_knowledge_schema = first_schema["managed_knowledge_item"]
        managed_knowledge_columns = dict(managed_knowledge_schema["columns"])
        assert managed_knowledge_columns["knowledge_key"] is True
        assert managed_knowledge_columns["content_hash"] is True
        assert "legacy_item_marker" in managed_knowledge_columns
        assert "AUTOINCREMENT" in managed_knowledge_schema["sql"].upper()
        assert "AUTOINCREMENT" in knowledge_base_schema["sql"].upper()
        assert (
            "uq_knowledge_base_id_uid",
            ("id", "uid"),
        ) in knowledge_base_schema["unique_constraints"]

        assert before["managed_knowledge_item"]["foreign_keys"] == (
            (
                ("knowledge_base_id", "uid"),
                "knowledge_base",
                ("id", "uid"),
                "CASCADE",
            ),
        )
        assert managed_knowledge_schema["foreign_keys"] == before["managed_knowledge_item"]["foreign_keys"] == second_schema["managed_knowledge_item"]["foreign_keys"]
        assert managed_knowledge_schema["foreign_keys"] == (
            (
                ("knowledge_base_id", "uid"),
                "knowledge_base",
                ("id", "uid"),
                "CASCADE",
            ),
        )
        assert set(managed_knowledge_schema["unique_constraints"]) == {
            (
                "uq_managed_knowledge_item_kb_key",
                ("knowledge_base_id", "knowledge_key"),
            ),
            (
                "uq_managed_knowledge_item_kb_content_hash",
                ("knowledge_base_id", "content_hash"),
            ),
        }
        assert (
            "ix_managed_knowledge_item_legacy_content",
            ("content",),
            False,
        ) in managed_knowledge_schema["indexes"]

        current_knowledge_base = dict((await session.execute(text("SELECT id, uid, name, legacy_kb_marker FROM knowledge_base WHERE id = 1"))).mappings().one())
        current_item = dict((await session.execute(text("SELECT id, knowledge_base_id, uid, knowledge_key, content, content_hash, legacy_item_marker FROM managed_knowledge_item WHERE id = 1"))).mappings().one())
        assert current_knowledge_base == original_knowledge_base
        assert current_item == original_item
        assert await _foreign_keys_enabled(session)
        assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []

        await session.execute(
            text("INSERT INTO knowledge_base (uid, name, legacy_kb_marker) VALUES (:uid, :name, :legacy_kb_marker)"),
            {
                "uid": "cascade-user",
                "name": "cascade-base",
                "legacy_kb_marker": "cascade-kb-marker",
            },
        )
        cascade_knowledge_base_id = int(
            (
                await session.execute(
                    text("SELECT id FROM knowledge_base WHERE name = :name"),
                    {"name": "cascade-base"},
                )
            ).scalar_one()
        )
        await session.execute(
            text("INSERT INTO managed_knowledge_item (knowledge_base_id, uid, knowledge_key, content, content_hash, legacy_item_marker) VALUES (:knowledge_base_id, :uid, :knowledge_key, :content, :content_hash, :legacy_item_marker)"),
            {
                "knowledge_base_id": cascade_knowledge_base_id,
                "uid": "cascade-user",
                "knowledge_key": "cascade-key",
                "content": "cascade-content",
                "content_hash": "cascade-hash",
                "legacy_item_marker": "cascade-item-marker",
            },
        )
        cascade_child_id = int(
            (
                await session.execute(
                    text("SELECT id FROM managed_knowledge_item WHERE content = :content"),
                    {"content": "cascade-content"},
                )
            ).scalar_one()
        )
        await session.commit()

        assert await _foreign_keys_enabled(session)
        await session.execute(
            text("DELETE FROM knowledge_base WHERE id = :knowledge_base_id"),
            {"knowledge_base_id": cascade_knowledge_base_id},
        )
        await session.commit()
        assert (
            await session.execute(
                text("SELECT COUNT(*) FROM managed_knowledge_item WHERE id = :id"),
                {"id": cascade_child_id},
            )
        ).scalar_one() == 0

        await session.execute(
            text("INSERT INTO managed_knowledge_item (knowledge_base_id, uid, knowledge_key, content, content_hash, legacy_item_marker) VALUES (:knowledge_base_id, :uid, NULL, :content, NULL, :legacy_item_marker)"),
            {
                "knowledge_base_id": 1,
                "uid": "legacy-user",
                "content": "null-content-1",
                "legacy_item_marker": "null-item-1",
            },
        )
        first_new_id = int(
            (
                await session.execute(
                    text("SELECT id FROM managed_knowledge_item WHERE content = :content"),
                    {"content": "null-content-1"},
                )
            ).scalar_one()
        )
        await session.execute(
            text("INSERT INTO managed_knowledge_item (knowledge_base_id, uid, knowledge_key, content, content_hash, legacy_item_marker) VALUES (:knowledge_base_id, :uid, NULL, :content, NULL, :legacy_item_marker)"),
            {
                "knowledge_base_id": 1,
                "uid": "legacy-user",
                "content": "null-content-2",
                "legacy_item_marker": "null-item-2",
            },
        )
        second_new_id = int(
            (
                await session.execute(
                    text("SELECT id FROM managed_knowledge_item WHERE content = :content"),
                    {"content": "null-content-2"},
                )
            ).scalar_one()
        )
        await session.commit()

        assert original_item["id"] < first_new_id < second_new_id
        new_rows = (
            (
                await session.execute(
                    text("SELECT id, knowledge_key, content_hash, legacy_item_marker FROM managed_knowledge_item WHERE id IN (:first_id, :second_id) ORDER BY id"),
                    {"first_id": first_new_id, "second_id": second_new_id},
                )
            )
            .mappings()
            .all()
        )
        assert [row["id"] for row in new_rows] == [first_new_id, second_new_id]
        assert [row["knowledge_key"] for row in new_rows] == [None, None]
        assert [row["content_hash"] for row in new_rows] == [None, None]
        assert [row["legacy_item_marker"] for row in new_rows] == ["null-item-1", "null-item-2"]
        assert await _foreign_keys_enabled(session)
