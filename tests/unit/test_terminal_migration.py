import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts import migration_20260731_add_terminal_sessions as migration


@pytest.mark.parametrize(
    ("dialect_name", "expected_json", "expected_datetime"),
    [
        ("postgresql", "JSONB", "TIMESTAMP WITH TIME ZONE"),
        ("mysql", "JSON", "DATETIME(6)"),
        ("sqlite", "JSON", "DATETIME"),
    ],
)
def test_terminal_migration_uses_expected_json_and_datetime_types(dialect_name, expected_json, expected_datetime):
    types = migration._column_types(dialect_name)

    assert types["json"] == expected_json
    assert types["datetime"] == expected_datetime


@pytest.mark.parametrize(
    ("dialect_name", "expected_id_type"),
    [
        ("postgresql", "SERIAL PRIMARY KEY"),
        ("mysql", "INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY"),
        ("sqlite", "INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT"),
    ],
)
def test_terminal_migration_uses_expected_auto_increment_id_type(dialect_name, expected_id_type):
    assert migration._id_type(dialect_name) == expected_id_type


@pytest.mark.asyncio
async def test_terminal_migration_creates_complete_idempotent_schema():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            await migration.migrate(session)
            await session.commit()
            await migration.migrate(session)
            await session.commit()

        async with engine.connect() as connection:
            schema = await connection.run_sync(_inspect_terminal_schema)

        assert schema["table_names"] >= {"terminal_session", "terminal_control_command"}
        assert schema["session_columns"] == {
            "terminal_session_id",
            "uid",
            "session_id",
            "original_tool_call_id",
            "profile_id",
            "audit_record_id",
            "audit_execution_record_id",
            "command",
            "working_directory",
            "status",
            "allowed_actions",
            "output_capacity_bytes",
            "oldest_output_offset",
            "next_output_offset",
            "oldest_output_sequence",
            "next_output_sequence",
            "exit_code",
            "failure_reason",
            "locked_by",
            "lock_until",
            "created_at",
            "updated_at",
            "started_at",
            "finished_at",
        }
        assert schema["control_columns"] == {
            "id",
            "terminal_session_id",
            "request_id",
            "action",
            "payload",
            "payload_hash",
            "status",
            "locked_by",
            "lock_until",
            "result",
            "error",
            "created_at",
            "updated_at",
            "finished_at",
        }
        assert ("audit_execution_record_id",) in schema["session_unique_columns"]
        assert ("terminal_session_id", "request_id") in schema["control_unique_columns"]
        assert {"uid", "status", "lock_until"} <= schema["session_index_columns"]
        assert {"terminal_session_id", "status", "lock_until"} <= schema["control_index_columns"]
    finally:
        await engine.dispose()


def _inspect_terminal_schema(sync_connection):
    inspector = inspect(sync_connection)

    def unique_columns(table_name):
        indexes = {tuple(index.get("column_names") or []) for index in inspector.get_indexes(table_name) if index.get("unique")}
        constraints = {tuple(constraint.get("column_names") or []) for constraint in inspector.get_unique_constraints(table_name)}
        return indexes | constraints

    def index_columns(table_name):
        return {column_name for index in inspector.get_indexes(table_name) for column_name in index.get("column_names") or [] if not index.get("unique")}

    return {
        "table_names": set(inspector.get_table_names()),
        "session_columns": {column["name"] for column in inspector.get_columns("terminal_session")},
        "control_columns": {column["name"] for column in inspector.get_columns("terminal_control_command")},
        "session_unique_columns": unique_columns("terminal_session"),
        "control_unique_columns": unique_columns("terminal_control_command"),
        "session_index_columns": index_columns("terminal_session"),
        "control_index_columns": index_columns("terminal_control_command"),
    }
