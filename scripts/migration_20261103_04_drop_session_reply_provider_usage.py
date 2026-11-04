from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261103_04_drop_session_reply_provider_usage"

_PREVIOUS_MIGRATION_ID = "20261103_03_consolidate_provider_usage"


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)

    previous_migration = await session.execute(
        text("SELECT 1 FROM migration_record WHERE migration_id = :migration_id LIMIT 1"),
        {"migration_id": _PREVIOUS_MIGRATION_ID},
    )
    if previous_migration.scalar_one_or_none() is None:
        raise RuntimeError(f"{MIGRATION_ID}: cannot drop session_reply_provider_usage because migration {_PREVIOUS_MIGRATION_ID} has not been committed")

    await session.execute(text("DROP TABLE IF EXISTS session_reply_provider_usage"))
