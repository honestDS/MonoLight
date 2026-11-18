from sqlalchemy import JSON, Integer, String, column, select, table, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261118_01_add_message_platform_merge_wait"


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)
    message_platform = table(
        "message_platform",
        column("id", Integer),
        column("platform_type", String),
        column("config", JSON),
    )
    result = await session.execute(select(message_platform.c.id, message_platform.c.config).where(message_platform.c.platform_type == "WEIXIN_OPENCLAW").with_for_update())

    for row in result:
        config = row.config
        if config is None:
            config = {}
        elif not isinstance(config, dict):
            continue

        message_merge_wait_ms = config.get("message_merge_wait_ms")
        if type(message_merge_wait_ms) is int and 1 <= message_merge_wait_ms <= 15_000:
            continue

        config = dict(config)
        config["message_merge_wait_ms"] = 5_000
        await session.execute(update(message_platform).where(message_platform.c.id == row.id).values(config=config))
