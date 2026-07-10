from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_event import SessionEvent

MIGRATION_ID = "20260710_add_session_event"


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(lambda sync_connection: SessionEvent.__table__.create(sync_connection, checkfirst=True))
