from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_reply_work_item import SessionReplyWorkItem
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork
from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261105_02_add_subagents"


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)

    connection = await session.connection()
    work_columns = await connection.run_sync(lambda sync_connection: {column["name"] for column in inspect(sync_connection).get_columns("session_reply_work_item")})
    if "subagent_id" not in work_columns:
        await session.execute(text("ALTER TABLE session_reply_work_item ADD COLUMN subagent_id VARCHAR(32) NULL"))

    subagent_index = next(index for index in SessionReplyWorkItem.__table__.indexes if [column.name for column in index.columns] == ["subagent_id"])
    await connection.run_sync(lambda sync_connection: subagent_index.create(sync_connection, checkfirst=True))

    for model in (Subagent, SubagentWork, SubagentToolCall):
        await connection.run_sync(lambda sync_connection, table=model.__table__: table.create(sync_connection, checkfirst=True))
