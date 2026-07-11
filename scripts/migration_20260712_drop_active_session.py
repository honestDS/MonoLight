from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260712_drop_active_session"


async def migrate(session: AsyncSession) -> None:
    await session.execute(text("DROP TABLE IF EXISTS active_session"))
