import json
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.audit import build_audit_status_update, build_passed_execution_claim_update, build_pending_execution_claim_update
from app.core.utils.time import get_local_time
from app.models.audit import AuditRecord, AuditRecordStatus
from app.providers.database.bootstrap import ensure_migration_record_table
from scripts import migration_20260717_add_audit_confirmation_records as audit_migration
from scripts import migration_20260719_add_background_task_audit_binding as background_task_migration
from scripts import migration_20260724_add_audit_tool_result_versions as audit_tool_result_version_migration
from scripts import migration_20260727_add_channel_http_proxy as channel_http_proxy_migration
from scripts import migration_20260727_drop_channel_type as drop_channel_type_migration


@pytest.mark.parametrize(
    ("dialect_name", "expected_id", "expected_datetime", "expected_json"),
    [
        ("sqlite", "AUTOINCREMENT", "DATETIME", "JSON"),
        ("mysql", "AUTO_INCREMENT", "DATETIME(6)", "JSON"),
    ],
)
def test_audit_migration_has_supported_database_types(dialect_name, expected_id, expected_datetime, expected_json):
    types = audit_migration._column_types(dialect_name)

    assert expected_id in types["id"]
    assert types["datetime"] == expected_datetime
    assert types["json"] == expected_json


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dialect_name", "expected_id"),
    [
        ("sqlite", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("mysql", "INTEGER PRIMARY KEY AUTO_INCREMENT"),
    ],
)
async def test_migration_record_table_uses_supported_database_identity(dialect_name, expected_id):
    statements = []

    class FakeSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))

        async def execute(self, statement):
            statements.append(str(statement))

        async def commit(self):
            return None

    await ensure_migration_record_table(FakeSession())

    assert expected_id in statements[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect_name", ["sqlite", "mysql"])
async def test_audit_migration_builds_all_tables_for_supported_databases(monkeypatch, dialect_name):
    statements = []

    class FakeSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))

        async def execute(self, statement):
            statements.append(str(statement))

    async def skip_indexes(session, table_name, indexes):
        return None

    monkeypatch.setattr(audit_migration, "_ensure_indexes", skip_indexes)

    await audit_migration.migrate(FakeSession())

    combined = "\n".join(statements)
    assert "CREATE TABLE IF NOT EXISTS audit_record" in combined
    assert "CREATE TABLE IF NOT EXISTS audit_tool_detail" in combined
    assert "CREATE TABLE IF NOT EXISTS audit_confirmation_claim" in combined
    assert "CREATE TABLE IF NOT EXISTS audit_execution_record" in combined


@pytest.mark.parametrize("dialect", [sqlite.dialect(), mysql.dialect()])
def test_all_audit_conditional_writes_compile_without_dialect_specific_syntax(dialect):
    now = get_local_time()
    statements = [
        build_audit_status_update(1, AuditRecordStatus.PREPARING, status=AuditRecordStatus.PENDING, updated_at=now),
        build_pending_execution_claim_update(
            audit_record_id=1,
            uid="u1",
            session_id="s1",
            now=now,
            claim_token="pending-token",
            decision_message_id=2,
            decision_raw_message="approve",
            decided_by="tester",
        ),
        build_passed_execution_claim_update(
            audit_record_id=1,
            now=now,
            claim_token="passed-token",
        ),
    ]

    for statement in statements:
        compiled = str(statement.compile(dialect=dialect)).upper()
        assert compiled.startswith("UPDATE AUDIT_RECORD")
        assert "WHERE" in compiled
        assert "RETURNING" not in compiled
        assert "ON CONFLICT" not in compiled
        assert "INSERT IGNORE" not in compiled


@pytest.mark.asyncio
async def test_audit_migration_runs_on_sqlite(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            await audit_migration.migrate(session)
            await session.commit()
        async with engine.connect() as connection:
            table_names, execution_indexes = await connection.run_sync(
                lambda sync_connection: (
                    set(inspect(sync_connection).get_table_names()),
                    {item["name"] for item in inspect(sync_connection).get_indexes("audit_execution_record")},
                )
            )
        assert {"audit_record", "audit_tool_detail", "audit_confirmation_claim", "audit_execution_record"}.issubset(table_names)
        assert "ix_audit_execution_record_claim_token" in execution_indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_background_task_binding_migration_is_idempotent_after_metadata_create_all(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'background-task-migration.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with session_factory() as session:
            await background_task_migration.migrate(session)
            await session.commit()
            await background_task_migration.migrate(session)
            await session.commit()
        async with engine.connect() as connection:
            unique_indexes = await connection.run_sync(lambda sync_connection: [item for item in inspect(sync_connection).get_indexes("background_task") if item.get("unique") and item.get("column_names") == ["audit_execution_record_id"]])
        assert len(unique_indexes) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_audit_tool_result_version_migration_is_idempotent_after_metadata_create_all(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audit-tool-result-version-migration.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        async with session_factory() as session:
            await audit_tool_result_version_migration.migrate(session)
            await session.commit()
            await audit_tool_result_version_migration.migrate(session)
            await session.commit()
        async with engine.connect() as connection:
            table_names, indexes = await connection.run_sync(
                lambda sync_connection: (
                    set(inspect(sync_connection).get_table_names()),
                    {item["name"] for item in inspect(sync_connection).get_indexes("audit_tool_result_version")},
                )
            )
        assert "audit_tool_result_version" in table_names
        assert "ix_audit_tool_result_version_audit_record_id" in indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_drop_channel_type_migration_is_idempotent_on_sqlite_legacy_channel(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'drop-channel-type-migration.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE channel (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, channel_type VARCHAR NOT NULL)"))
        async with session_factory() as session:
            await drop_channel_type_migration.migrate(session)
            await session.commit()
            await drop_channel_type_migration.migrate(session)
            await session.commit()
        async with engine.connect() as connection:
            column_names = await connection.run_sync(lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("channel")})
        assert "channel_type" not in column_names
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_channel_http_proxy_migration_promotes_only_unambiguous_legacy_proxy_on_sqlite(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'channel-http-proxy-migration.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    single_proxy_model_ids = json.dumps(
        [
            {
                "model_id": "single-proxy-model",
                "advanced_settings": {
                    "http_proxy": "http://proxy.example.com:8080",
                    "future_extension": {"enabled": True},
                },
            }
        ],
        indent=2,
    )
    conflicting_proxy_model_ids = json.dumps(
        [
            {
                "model_id": "first-model",
                "advanced_settings": {"http_proxy": "http://first-proxy.example.com:8080"},
            },
            {
                "model_id": "second-model",
                "advanced_settings": {"http_proxy": "http://second-proxy.example.com:8080"},
            },
        ],
        separators=(", ", ": "),
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE channel (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, model_ids TEXT)"))
            await connection.execute(
                text("INSERT INTO channel (id, name, model_ids) VALUES (:id, :name, :model_ids)"),
                [
                    {"id": 1, "name": "single-proxy", "model_ids": single_proxy_model_ids},
                    {"id": 2, "name": "conflicting-proxies", "model_ids": conflicting_proxy_model_ids},
                ],
            )

        async with session_factory() as session:
            await channel_http_proxy_migration.migrate(session)
            await session.commit()
            await channel_http_proxy_migration.migrate(session)
            await session.commit()

        async with engine.connect() as connection:
            column_names = await connection.run_sync(lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("channel")})
            rows = (await connection.execute(text("SELECT id, model_ids, http_proxy FROM channel ORDER BY id"))).mappings().all()

        assert "http_proxy" in column_names
        assert rows[0]["http_proxy"] == "http://proxy.example.com:8080"
        assert rows[1]["http_proxy"] is None
        assert rows[0]["model_ids"] == single_proxy_model_ids
        assert rows[1]["model_ids"] == conflicting_proxy_model_ids
    finally:
        await engine.dispose()
