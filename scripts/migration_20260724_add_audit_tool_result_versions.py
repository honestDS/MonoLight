from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260724_add_audit_tool_result_versions"


def _column_types(dialect_name: str) -> dict[str, str]:
    if dialect_name == "sqlite":
        return {
            "id": "INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT",
            "datetime": "DATETIME",
            "text": "TEXT",
        }
    if dialect_name == "mysql":
        return {
            "id": "INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY",
            "datetime": "DATETIME(6)",
            "text": "LONGTEXT",
        }
    raise RuntimeError("Unsupported SQL dialect")


async def _existing_columns(session: AsyncSession, table_name: str) -> set[str]:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: {str(item["name"]) for item in inspect(sync_connection).get_columns(table_name)})


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
    message_columns = await _existing_columns(session, "message")
    if "audit_record_id" not in message_columns:
        await session.execute(text("ALTER TABLE message ADD COLUMN audit_record_id INTEGER"))
    if "audit_tool_call_id" not in message_columns:
        await session.execute(text("ALTER TABLE message ADD COLUMN audit_tool_call_id VARCHAR(100)"))
    if "content_revision" not in message_columns:
        await session.execute(text("ALTER TABLE message ADD COLUMN content_revision INTEGER NOT NULL DEFAULT 0"))
    await session.execute(text("UPDATE message SET content_revision = 0 WHERE content_revision IS NULL"))

    session_columns = await _existing_columns(session, "chat_session")
    if "context_content_revision" not in session_columns:
        await session.execute(text("ALTER TABLE chat_session ADD COLUMN context_content_revision INTEGER NOT NULL DEFAULT 0"))
    await session.execute(text("UPDATE chat_session SET context_content_revision = 0 WHERE context_content_revision IS NULL"))

    stage_columns = await _existing_columns(session, "context_summary_stage")
    if "expected_content_revision" not in stage_columns:
        await session.execute(text("ALTER TABLE context_summary_stage ADD COLUMN expected_content_revision INTEGER NOT NULL DEFAULT 0"))
    await session.execute(text("UPDATE context_summary_stage SET expected_content_revision = 0 WHERE expected_content_revision IS NULL"))

    dialect_name = session.get_bind().dialect.name
    types = _column_types(dialect_name)
    await session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS audit_tool_result_version (
                id {types["id"]},
                uid VARCHAR(100) NOT NULL,
                session_id VARCHAR(100) NOT NULL,
                audit_record_id INTEGER NOT NULL,
                source_assistant_message_id INTEGER NOT NULL,
                original_tool_call_id VARCHAR(100) NOT NULL,
                message_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                content {types["text"]} NOT NULL,
                created_at {types["datetime"]} NOT NULL,
                CONSTRAINT uq_audit_tool_result_version_record_call_version
                    UNIQUE (audit_record_id, original_tool_call_id, version_no)
            )
            """
        )
    )

    await _ensure_indexes(
        session,
        "message",
        [
            ("ix_message_audit_record_id", "audit_record_id"),
            ("ix_message_audit_tool_call_id", "audit_tool_call_id"),
        ],
    )
    await _ensure_indexes(
        session,
        "audit_tool_result_version",
        [
            ("ix_audit_tool_result_version_id", "id"),
            ("ix_audit_tool_result_version_uid", "uid"),
            ("ix_audit_tool_result_version_session_id", "session_id"),
            ("ix_audit_tool_result_version_audit_record_id", "audit_record_id"),
            ("ix_audit_tool_result_version_source_assistant_message_id", "source_assistant_message_id"),
            ("ix_audit_tool_result_version_original_tool_call_id", "original_tool_call_id"),
            ("ix_audit_tool_result_version_message_id", "message_id"),
            ("ix_audit_tool_result_version_created_at", "created_at"),
        ],
    )
    if not await _has_unique_columns(
        session,
        "audit_tool_result_version",
        ["audit_record_id", "original_tool_call_id", "version_no"],
    ):
        await session.execute(text("CREATE UNIQUE INDEX uq_audit_tool_result_version_record_call_version ON audit_tool_result_version (audit_record_id, original_tool_call_id, version_no)"))
