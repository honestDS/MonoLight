from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, update
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.crud.audit import build_audit_status_update, build_passed_execution_claim_update, build_pending_execution_claim_update
from app.core.utils.time import get_local_time
from app.models.audit import AuditRecord, AuditRecordStatus
from app.providers.database.bootstrap import ensure_migration_record_table
from scripts import migration_20260717_add_audit_confirmation_records as audit_migration


@pytest.mark.parametrize(
    ("dialect_name", "expected_id", "expected_datetime", "expected_json"),
    [
        ("sqlite", "AUTOINCREMENT", "DATETIME", "JSON"),
        ("mysql", "AUTO_INCREMENT", "DATETIME(6)", "JSON"),
        ("postgresql", "SERIAL PRIMARY KEY", "TIMESTAMP WITH TIME ZONE", "JSONB"),
    ],
)
def test_audit_migration_has_database_specific_types(dialect_name, expected_id, expected_datetime, expected_json):
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
        ("postgresql", "SERIAL PRIMARY KEY"),
    ],
)
async def test_migration_record_table_uses_database_specific_identity(dialect_name, expected_id):
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
@pytest.mark.parametrize("dialect_name", ["sqlite", "mysql", "postgresql"])
async def test_audit_migration_builds_all_tables_for_each_database(monkeypatch, dialect_name):
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


@pytest.mark.parametrize("dialect", [sqlite.dialect(), mysql.dialect(), postgresql.dialect()])
def test_audit_conditional_claim_update_compiles_for_each_database(dialect):
    statement = (
        update(AuditRecord)
        .where(
            AuditRecord.id == 1,
            AuditRecord.status == AuditRecordStatus.PENDING,
        )
        .values(status=AuditRecordStatus.EXECUTING, execution_claim_token="claim-token")
    )

    compiled = str(statement.compile(dialect=dialect))

    assert "UPDATE audit_record" in compiled
    assert "WHERE audit_record.id" in compiled
    assert "audit_record.status" in compiled
    assert "RETURNING" not in compiled.upper()


@pytest.mark.parametrize("dialect", [sqlite.dialect(), mysql.dialect(), postgresql.dialect()])
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
