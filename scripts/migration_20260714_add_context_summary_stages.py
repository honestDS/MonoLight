from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260714_add_context_summary_stages"


async def _column_names(session: AsyncSession, table_name: str) -> set[str]:
    result = await session.execute(text(f"PRAGMA table_info({table_name})"))
    return {str(row[1]) for row in result.fetchall()}


async def migrate(session: AsyncSession) -> None:
    chat_session_columns = await _column_names(session, "chat_session")
    if "context_summary_revision" not in chat_session_columns:
        await session.execute(text("ALTER TABLE chat_session ADD COLUMN context_summary_revision INTEGER NOT NULL DEFAULT 0"))
    await session.execute(text("UPDATE chat_session SET context_summary_revision = 0 WHERE context_summary_revision IS NULL"))

    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS context_summary_stage (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                uid VARCHAR(100) NOT NULL,
                session_id VARCHAR(100) NOT NULL,
                work_id INTEGER NOT NULL,
                work_dedupe_key VARCHAR(160) NOT NULL,
                snapshot_key VARCHAR(64) NOT NULL,
                stage_key VARCHAR(64) NOT NULL,
                lower_stage_key VARCHAR(64),
                model_key VARCHAR(64) NOT NULL,
                channel_id INTEGER NOT NULL,
                model_id VARCHAR(255) NOT NULL,
                context_window_k INTEGER NOT NULL,
                max_output_tokens INTEGER NOT NULL,
                safety_margin_tokens INTEGER NOT NULL,
                expected_summary_message_id INTEGER,
                expected_summary_revision INTEGER NOT NULL,
                snapshot_max_message_id INTEGER NOT NULL,
                persistent_summary_target_id INTEGER NOT NULL,
                expected_fragment_count INTEGER NOT NULL,
                succeeded_fragment_count INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(20) NOT NULL DEFAULT 'running',
                error TEXT,
                created_at DATETIME NOT NULL,
                completed_at DATETIME,
                CONSTRAINT uq_context_summary_stage_work_stage
                    UNIQUE (work_dedupe_key, stage_key)
            )
            """
        )
    )
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS context_summary_fragment (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                dedupe_key VARCHAR(64) NOT NULL,
                uid VARCHAR(100) NOT NULL,
                session_id VARCHAR(100) NOT NULL,
                work_id INTEGER NOT NULL,
                work_dedupe_key VARCHAR(160) NOT NULL,
                snapshot_key VARCHAR(64) NOT NULL,
                stage_key VARCHAR(64) NOT NULL,
                model_key VARCHAR(64) NOT NULL,
                fragment_index INTEGER NOT NULL,
                message_start_id INTEGER NOT NULL,
                message_end_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                model_id VARCHAR(255) NOT NULL,
                token_count INTEGER NOT NULL,
                content TEXT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'completed',
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_context_summary_fragment_work_stage_index
                    UNIQUE (work_dedupe_key, stage_key, fragment_index),
                CONSTRAINT uq_context_summary_fragment_dedupe
                    UNIQUE (dedupe_key)
            )
            """
        )
    )

    stage_indexes = [
        ("ix_context_summary_stage_id", "id"),
        ("ix_context_summary_stage_uid", "uid"),
        ("ix_context_summary_stage_session_id", "session_id"),
        ("ix_context_summary_stage_work_id", "work_id"),
        ("ix_context_summary_stage_work_dedupe_key", "work_dedupe_key"),
        ("ix_context_summary_stage_snapshot_key", "snapshot_key"),
        ("ix_context_summary_stage_stage_key", "stage_key"),
        ("ix_context_summary_stage_lower_stage_key", "lower_stage_key"),
        ("ix_context_summary_stage_model_key", "model_key"),
        ("ix_context_summary_stage_channel_id", "channel_id"),
        ("ix_context_summary_stage_status", "status"),
        ("ix_context_summary_stage_created_at", "created_at"),
        ("ix_context_summary_stage_completed_at", "completed_at"),
    ]
    for index_name, column_name in stage_indexes:
        await session.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON context_summary_stage ({column_name})"))

    fragment_indexes = [
        ("ix_context_summary_fragment_id", "id"),
        ("ix_context_summary_fragment_dedupe_key", "dedupe_key"),
        ("ix_context_summary_fragment_uid", "uid"),
        ("ix_context_summary_fragment_session_id", "session_id"),
        ("ix_context_summary_fragment_work_id", "work_id"),
        (
            "ix_context_summary_fragment_work_dedupe_key",
            "work_dedupe_key",
        ),
        ("ix_context_summary_fragment_snapshot_key", "snapshot_key"),
        ("ix_context_summary_fragment_stage_key", "stage_key"),
        ("ix_context_summary_fragment_model_key", "model_key"),
        ("ix_context_summary_fragment_channel_id", "channel_id"),
        ("ix_context_summary_fragment_status", "status"),
        ("ix_context_summary_fragment_created_at", "created_at"),
    ]
    for index_name, column_name in fragment_indexes:
        await session.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON context_summary_fragment ({column_name})"))
