from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message_platform_outbox import MessagePlatformOutbox

MIGRATION_ID = "20260710_add_message_platform_outbox"


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(lambda sync_connection: MessagePlatformOutbox.__table__.create(sync_connection, checkfirst=True))
