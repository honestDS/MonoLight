from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261118_01_add_subagent_board_revision"


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)

    # 初始化阶段会先通过 create_all 创建全新表；本脚本只在同一事务中登记既有看板，和迁移记录一起提交或回滚。
    await session.execute(
        text(
            """
            INSERT INTO session_subagent_board_revision (session_id, revision)
            SELECT DISTINCT s.session_id, 0
            FROM subagent AS s
            JOIN chat_session AS c
              ON c.session_id = s.session_id
             AND c.uid = s.uid
            LEFT JOIN session_subagent_board_revision AS r
              ON r.session_id = s.session_id
            WHERE r.session_id IS NULL
            """
        )
    )
