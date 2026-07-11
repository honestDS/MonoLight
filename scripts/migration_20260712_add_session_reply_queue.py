from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260712_add_session_reply_queue"


async def migrate(session: AsyncSession) -> None:
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS session_reply_sequence (
                session_id VARCHAR(100) NOT NULL PRIMARY KEY,
                next_sequence_no INTEGER NOT NULL DEFAULT 1,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS session_reply_work_item (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                uid VARCHAR(100) NOT NULL,
                session_id VARCHAR(100) NOT NULL,
                profile_id INTEGER NOT NULL,
                sequence_no INTEGER NOT NULL,
                work_type VARCHAR(40) NOT NULL,
                source_type VARCHAR(40) NOT NULL,
                source_id VARCHAR(100) NOT NULL,
                dedupe_key VARCHAR(160) NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'ready_for_llm',
                merged_into_id INTEGER,
                input_message_ids JSON,
                result_message_id INTEGER,
                execution_state JSON NOT NULL DEFAULT '{}',
                event_sent BOOLEAN NOT NULL DEFAULT 0,
                locked_by VARCHAR(100),
                lock_until INTEGER,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                available_at INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_session_reply_work_sequence UNIQUE (session_id, sequence_no),
                CONSTRAINT uq_session_reply_work_dedupe UNIQUE (dedupe_key)
            )
            """
        )
    )
    indexes = [
        ("ix_session_reply_work_item_uid", "uid"),
        ("ix_session_reply_work_item_session_id", "session_id"),
        ("ix_session_reply_work_item_profile_id", "profile_id"),
        ("ix_session_reply_work_item_sequence_no", "sequence_no"),
        ("ix_session_reply_work_item_work_type", "work_type"),
        ("ix_session_reply_work_item_source_type", "source_type"),
        ("ix_session_reply_work_item_source_id", "source_id"),
        ("ix_session_reply_work_item_dedupe_key", "dedupe_key"),
        ("ix_session_reply_work_item_status", "status"),
        ("ix_session_reply_work_item_merged_into_id", "merged_into_id"),
        ("ix_session_reply_work_item_result_message_id", "result_message_id"),
        ("ix_session_reply_work_item_locked_by", "locked_by"),
        ("ix_session_reply_work_item_lock_until", "lock_until"),
        ("ix_session_reply_work_item_available_at", "available_at"),
        ("ix_session_reply_work_item_created_at", "created_at"),
        ("ix_session_reply_work_item_updated_at", "updated_at"),
    ]
    for index_name, column_name in indexes:
        await session.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON session_reply_work_item ({column_name})"))
