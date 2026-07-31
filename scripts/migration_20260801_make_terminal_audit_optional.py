from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260801_make_terminal_audit_optional"

TERMINAL_SESSION_COLUMNS = (
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
)

TERMINAL_SESSION_INDEXES = (
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
)


async def _table_exists(session: AsyncSession) -> bool:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: inspect(sync_connection).has_table("terminal_session"))


async def _rebuild_sqlite_table(session: AsyncSession) -> None:
    column_list = ", ".join(TERMINAL_SESSION_COLUMNS)

    await session.execute(text("DROP TABLE IF EXISTS terminal_session_audit_optional_new"))
    await session.execute(
        text(
            """
            CREATE TABLE terminal_session_audit_optional_new (
                terminal_session_id VARCHAR(128) NOT NULL PRIMARY KEY,
                uid VARCHAR(100) NOT NULL,
                session_id VARCHAR(100) NOT NULL,
                original_tool_call_id VARCHAR(100) NOT NULL,
                profile_id INTEGER NOT NULL,
                audit_record_id INTEGER,
                audit_execution_record_id INTEGER,
                command TEXT NOT NULL,
                working_directory TEXT NOT NULL,
                status VARCHAR(20) NOT NULL,
                allowed_actions JSON NOT NULL,
                output_capacity_bytes INTEGER NOT NULL DEFAULT 1048576,
                oldest_output_offset INTEGER NOT NULL DEFAULT 0,
                next_output_offset INTEGER NOT NULL DEFAULT 0,
                oldest_output_sequence INTEGER NOT NULL DEFAULT 1,
                next_output_sequence INTEGER NOT NULL DEFAULT 1,
                exit_code INTEGER,
                failure_reason TEXT,
                locked_by VARCHAR(100),
                lock_until INTEGER,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                started_at DATETIME,
                finished_at DATETIME,
                CONSTRAINT uq_terminal_session_audit_execution_record_id
                    UNIQUE (audit_execution_record_id)
            )
            """
        )
    )
    await session.execute(text(f"INSERT INTO terminal_session_audit_optional_new ({column_list}) SELECT {column_list} FROM terminal_session"))
    await session.execute(text("DROP TABLE terminal_session"))
    await session.execute(text("ALTER TABLE terminal_session_audit_optional_new RENAME TO terminal_session"))

    for index_name, column_name in TERMINAL_SESSION_INDEXES:
        await session.execute(text(f"CREATE INDEX {index_name} ON terminal_session ({column_name})"))


async def migrate(session: AsyncSession) -> None:
    if not await _table_exists(session):
        return

    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        await session.execute(text("ALTER TABLE terminal_session ALTER COLUMN audit_record_id DROP NOT NULL"))
        await session.execute(text("ALTER TABLE terminal_session ALTER COLUMN audit_execution_record_id DROP NOT NULL"))
    elif dialect_name == "mysql":
        await session.execute(text("ALTER TABLE terminal_session MODIFY COLUMN audit_record_id INTEGER NULL"))
        await session.execute(text("ALTER TABLE terminal_session MODIFY COLUMN audit_execution_record_id INTEGER NULL"))
    elif dialect_name == "sqlite":
        await _rebuild_sqlite_table(session)
