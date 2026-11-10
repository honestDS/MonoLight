from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subagent import SubagentBoardPost
from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261109_02_add_subagent_board_post"


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)

    connection = await session.connection()
    await connection.run_sync(
        lambda sync_connection, table=SubagentBoardPost.__table__: table.create(sync_connection, checkfirst=True)
    )
