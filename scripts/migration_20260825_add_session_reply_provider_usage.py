from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_reply_provider_usage import SessionReplyProviderUsage

MIGRATION_ID = "20260825_add_session_reply_provider_usage_v1"


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(
        lambda sync_connection: SessionReplyProviderUsage.__table__.create(
            sync_connection,
            checkfirst=True,
        )
    )
