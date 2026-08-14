from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260717_add_audit_confirmation_records"


def _column_types(dialect_name: str) -> dict[str, str]:
    if dialect_name == "sqlite":
        return {
            "id": "INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT",
            "datetime": "DATETIME",
            "json": "JSON",
            "text": "TEXT",
        }
    if dialect_name == "mysql":
        return {
            "id": "INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY",
            "datetime": "DATETIME(6)",
            "json": "JSON",
            "text": "LONGTEXT",
        }
    raise RuntimeError("Unsupported SQL dialect")


async def _existing_index_names(session: AsyncSession, table_name: str) -> set[str]:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: {str(item["name"]) for item in inspect(sync_connection).get_indexes(table_name)})


async def _ensure_indexes(session: AsyncSession, table_name: str, indexes: list[tuple[str, str]]) -> None:
    existing_names = await _existing_index_names(session, table_name)
    for index_name, columns in indexes:
        if index_name in existing_names:
            continue
        await session.execute(text(f"CREATE INDEX {index_name} ON {table_name} ({columns})"))


async def migrate(session: AsyncSession) -> None:
    dialect_name = session.get_bind().dialect.name
    types = _column_types(dialect_name)
    id_type = types["id"]
    datetime_type = types["datetime"]
    json_type = types["json"]
    text_type = types["text"]

    await session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS audit_record (
                id {id_type},
                uid VARCHAR(100) NOT NULL,
                operator_username VARCHAR(100) NOT NULL,
                session_id VARCHAR(100) NOT NULL,
                source VARCHAR(40) NOT NULL,
                language VARCHAR(20) NOT NULL,
                status VARCHAR(30) NOT NULL,
                failure_type VARCHAR(40),
                error_reason {text_type},
                source_assistant_message_id INTEGER NOT NULL,
                working_directory {text_type} NOT NULL,
                round_arguments_hash VARCHAR(64) NOT NULL,
                tool_count INTEGER NOT NULL,
                intent_summary {text_type},
                context_file_path {text_type},
                decision VARCHAR(20),
                decision_message_id INTEGER,
                decision_raw_message {text_type},
                decided_by VARCHAR(100),
                execution_claim_token VARCHAR(64),
                created_at {datetime_type} NOT NULL,
                updated_at {datetime_type} NOT NULL,
                audited_at {datetime_type},
                pending_at {datetime_type},
                expires_at {datetime_type},
                decided_at {datetime_type},
                execution_started_at {datetime_type},
                completed_at {datetime_type}
            )
            """
        )
    )
    await session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS audit_tool_detail (
                id {id_type},
                audit_record_id INTEGER NOT NULL,
                original_tool_call_id VARCHAR(100) NOT NULL,
                turn_index INTEGER NOT NULL,
                tool_name VARCHAR(100) NOT NULL,
                conclusion VARCHAR(30) NOT NULL,
                score INTEGER,
                reason {text_type} NOT NULL,
                arguments_hash VARCHAR(64) NOT NULL,
                arguments_summary {text_type} NOT NULL,
                file_snapshots {json_type} NOT NULL,
                created_at {datetime_type} NOT NULL,
                CONSTRAINT uq_audit_tool_detail_record_call UNIQUE (audit_record_id, original_tool_call_id),
                CONSTRAINT uq_audit_tool_detail_record_turn UNIQUE (audit_record_id, turn_index)
            )
            """
        )
    )
    await session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS audit_confirmation_claim (
                id {id_type},
                uid VARCHAR(100) NOT NULL,
                session_id VARCHAR(100) NOT NULL,
                audit_record_id INTEGER NOT NULL,
                created_at {datetime_type} NOT NULL,
                CONSTRAINT uq_audit_confirmation_claim_user_session UNIQUE (uid, session_id),
                CONSTRAINT uq_audit_confirmation_claim_record UNIQUE (audit_record_id)
            )
            """
        )
    )
    await session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS audit_execution_record (
                id {id_type},
                audit_record_id INTEGER NOT NULL,
                audit_tool_detail_id INTEGER NOT NULL,
                attempt_no INTEGER NOT NULL,
                status VARCHAR(30) NOT NULL,
                claim_token VARCHAR(64) NOT NULL,
                execution_node VARCHAR(100) NOT NULL,
                new_tool_call_id VARCHAR(100) NOT NULL,
                result_summary {text_type},
                error {text_type},
                started_at {datetime_type} NOT NULL,
                finished_at {datetime_type},
                CONSTRAINT uq_audit_execution_detail_attempt UNIQUE (audit_tool_detail_id, attempt_no),
                CONSTRAINT uq_audit_execution_new_tool_call UNIQUE (new_tool_call_id)
            )
            """
        )
    )

    await _ensure_indexes(
        session,
        "audit_record",
        [
            ("ix_audit_record_id", "id"),
            ("ix_audit_record_uid", "uid"),
            ("ix_audit_record_session_id", "session_id"),
            ("ix_audit_record_source", "source"),
            ("ix_audit_record_status", "status"),
            ("ix_audit_record_failure_type", "failure_type"),
            ("ix_audit_record_source_assistant_message_id", "source_assistant_message_id"),
            ("ix_audit_record_round_arguments_hash", "round_arguments_hash"),
            ("ix_audit_record_decision_message_id", "decision_message_id"),
            ("ix_audit_record_execution_claim_token", "execution_claim_token"),
            ("ix_audit_record_created_at", "created_at"),
            ("ix_audit_record_updated_at", "updated_at"),
            ("ix_audit_record_expires_at", "expires_at"),
            ("ix_audit_record_completed_at", "completed_at"),
        ],
    )
    await _ensure_indexes(
        session,
        "audit_tool_detail",
        [
            ("ix_audit_tool_detail_id", "id"),
            ("ix_audit_tool_detail_audit_record_id", "audit_record_id"),
            ("ix_audit_tool_detail_original_tool_call_id", "original_tool_call_id"),
            ("ix_audit_tool_detail_tool_name", "tool_name"),
            ("ix_audit_tool_detail_conclusion", "conclusion"),
            ("ix_audit_tool_detail_arguments_hash", "arguments_hash"),
            ("ix_audit_tool_detail_created_at", "created_at"),
        ],
    )
    await _ensure_indexes(
        session,
        "audit_confirmation_claim",
        [
            ("ix_audit_confirmation_claim_id", "id"),
            ("ix_audit_confirmation_claim_uid", "uid"),
            ("ix_audit_confirmation_claim_session_id", "session_id"),
            ("ix_audit_confirmation_claim_audit_record_id", "audit_record_id"),
            ("ix_audit_confirmation_claim_created_at", "created_at"),
        ],
    )
    await _ensure_indexes(
        session,
        "audit_execution_record",
        [
            ("ix_audit_execution_record_id", "id"),
            ("ix_audit_execution_record_audit_record_id", "audit_record_id"),
            ("ix_audit_execution_record_audit_tool_detail_id", "audit_tool_detail_id"),
            ("ix_audit_execution_record_status", "status"),
            ("ix_audit_execution_record_claim_token", "claim_token"),
            ("ix_audit_execution_record_new_tool_call_id", "new_tool_call_id"),
            ("ix_audit_execution_record_started_at", "started_at"),
            ("ix_audit_execution_record_finished_at", "finished_at"),
        ],
    )
