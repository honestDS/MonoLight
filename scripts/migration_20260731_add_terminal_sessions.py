from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260731_add_terminal_sessions"


def _column_types(dialect_name: str) -> dict[str, str]:
    if dialect_name == "postgresql":
        return {
            "datetime": "TIMESTAMP WITH TIME ZONE",
            "json": "JSONB",
            "text": "TEXT",
        }
    if dialect_name == "mysql":
        return {
            "datetime": "DATETIME(6)",
            "json": "JSON",
            "text": "LONGTEXT",
        }
    return {
        "datetime": "DATETIME",
        "json": "JSON",
        "text": "TEXT",
    }


def _id_type(dialect_name: str) -> str:
    if dialect_name == "postgresql":
        return "SERIAL PRIMARY KEY"
    if dialect_name == "mysql":
        return "INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY"
    return "INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT"


async def _existing_index_names(session: AsyncSession, table_name: str) -> set[str]:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: {str(item["name"]) for item in inspect(sync_connection).get_indexes(table_name)})


async def _has_unique_columns(session: AsyncSession, table_name: str, column_names: list[str]) -> bool:
    connection = await session.connection()

    def inspect_unique(sync_connection) -> bool:
        inspector = inspect(sync_connection)
        expected = list(column_names)
        for index in inspector.get_indexes(table_name):
            if index.get("unique") and list(index.get("column_names") or []) == expected:
                return True
        for constraint in inspector.get_unique_constraints(table_name):
            if list(constraint.get("column_names") or []) == expected:
                return True
        return False

    return await connection.run_sync(inspect_unique)


async def _ensure_indexes(session: AsyncSession, table_name: str, indexes: list[tuple[str, str]]) -> None:
    existing_names = await _existing_index_names(session, table_name)
    for index_name, columns in indexes:
        if index_name not in existing_names:
            await session.execute(text(f"CREATE INDEX {index_name} ON {table_name} ({columns})"))


async def migrate(session: AsyncSession) -> None:
    dialect_name = session.get_bind().dialect.name
    types = _column_types(dialect_name)
    id_type = _id_type(dialect_name)
    datetime_type = types["datetime"]
    json_type = types["json"]
    text_type = types["text"]

    await session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS terminal_session (
                terminal_session_id VARCHAR(128) NOT NULL PRIMARY KEY,
                uid VARCHAR(100) NOT NULL,
                session_id VARCHAR(100) NOT NULL,
                original_tool_call_id VARCHAR(100) NOT NULL,
                profile_id INTEGER NOT NULL,
                audit_record_id INTEGER NOT NULL,
                audit_execution_record_id INTEGER NOT NULL,
                command {text_type} NOT NULL,
                working_directory {text_type} NOT NULL,
                status VARCHAR(20) NOT NULL,
                allowed_actions {json_type} NOT NULL,
                output_capacity_bytes INTEGER NOT NULL DEFAULT 1048576,
                oldest_output_offset INTEGER NOT NULL DEFAULT 0,
                next_output_offset INTEGER NOT NULL DEFAULT 0,
                oldest_output_sequence INTEGER NOT NULL DEFAULT 1,
                next_output_sequence INTEGER NOT NULL DEFAULT 1,
                exit_code INTEGER,
                failure_reason {text_type},
                locked_by VARCHAR(100),
                lock_until INTEGER,
                created_at {datetime_type} NOT NULL,
                updated_at {datetime_type} NOT NULL,
                started_at {datetime_type},
                finished_at {datetime_type},
                CONSTRAINT uq_terminal_session_audit_execution_record_id
                    UNIQUE (audit_execution_record_id)
            )
            """
        )
    )
    await session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS terminal_control_command (
                id {id_type},
                terminal_session_id VARCHAR(128) NOT NULL,
                request_id VARCHAR(128) NOT NULL,
                action VARCHAR(20) NOT NULL,
                payload {json_type} NOT NULL,
                payload_hash VARCHAR(64) NOT NULL,
                status VARCHAR(20) NOT NULL,
                locked_by VARCHAR(100),
                lock_until INTEGER,
                result {json_type},
                error {text_type},
                created_at {datetime_type} NOT NULL,
                updated_at {datetime_type} NOT NULL,
                finished_at {datetime_type},
                CONSTRAINT uq_terminal_control_command_session_request
                    UNIQUE (terminal_session_id, request_id)
            )
            """
        )
    )

    await _ensure_indexes(
        session,
        "terminal_session",
        [
            ("ix_terminal_session_terminal_session_id", "terminal_session_id"),
            ("ix_terminal_session_uid", "uid"),
            ("ix_terminal_session_session_id", "session_id"),
            ("ix_terminal_session_original_tool_call_id", "original_tool_call_id"),
            ("ix_terminal_session_profile_id", "profile_id"),
            ("ix_terminal_session_audit_record_id", "audit_record_id"),
            ("ix_terminal_session_audit_execution_record_id", "audit_execution_record_id"),
            ("ix_terminal_session_status", "status"),
            ("ix_terminal_session_locked_by", "locked_by"),
            ("ix_terminal_session_lock_until", "lock_until"),
            ("ix_terminal_session_created_at", "created_at"),
            ("ix_terminal_session_updated_at", "updated_at"),
            ("ix_terminal_session_finished_at", "finished_at"),
        ],
    )
    await _ensure_indexes(
        session,
        "terminal_control_command",
        [
            ("ix_terminal_control_command_id", "id"),
            ("ix_terminal_control_command_terminal_session_id", "terminal_session_id"),
            ("ix_terminal_control_command_request_id", "request_id"),
            ("ix_terminal_control_command_action", "action"),
            ("ix_terminal_control_command_payload_hash", "payload_hash"),
            ("ix_terminal_control_command_status", "status"),
            ("ix_terminal_control_command_locked_by", "locked_by"),
            ("ix_terminal_control_command_lock_until", "lock_until"),
            ("ix_terminal_control_command_created_at", "created_at"),
            ("ix_terminal_control_command_updated_at", "updated_at"),
            ("ix_terminal_control_command_finished_at", "finished_at"),
        ],
    )
    if not await _has_unique_columns(session, "terminal_session", ["audit_execution_record_id"]):
        await session.execute(text("CREATE UNIQUE INDEX uq_terminal_session_audit_execution_record_id ON terminal_session (audit_execution_record_id)"))
    if not await _has_unique_columns(session, "terminal_control_command", ["terminal_session_id", "request_id"]):
        await session.execute(text("CREATE UNIQUE INDEX uq_terminal_control_command_session_request ON terminal_control_command (terminal_session_id, request_id)"))
