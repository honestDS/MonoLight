from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260712_add_session_reply_stream_event"


async def migrate(session: AsyncSession) -> None:
    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS session_reply_stream_event (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL,
                sequence_no INTEGER NOT NULL,
                event JSON NOT NULL,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_session_reply_stream_event_sequence UNIQUE (work_id, sequence_no)
            )
            """
        )
    )
    indexes = [
        ("ix_session_reply_stream_event_work_id", "work_id"),
        ("ix_session_reply_stream_event_sequence_no", "sequence_no"),
        ("ix_session_reply_stream_event_created_at", "created_at"),
    ]
    for index_name, column_name in indexes:
        await session.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON session_reply_stream_event ({column_name})"))
