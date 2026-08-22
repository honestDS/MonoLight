"""知识库容器迁移入口。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t
from scripts.knowledge_base_container_migration_mysql import migrate_mysql
from scripts.knowledge_base_container_migration_sqlite import migrate_sqlite

MIGRATION_ID = "20260815_expand_knowledge_base_container_v1"


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
    if database_type not in {"sqlite", "mysql"}:
        raise RuntimeError(
            t(
                ERR_DATABASE_TYPE_UNSUPPORTED,
                database_type=database_type,
            )
        )

    if database_type == "sqlite":
        result = await connection.execute(text("PRAGMA foreign_keys"))
        foreign_keys_enabled = bool(result.scalar())
        try:
            await connection.run_sync(migrate_sqlite)
        except Exception:
            if foreign_keys_enabled:
                await session.rollback()
            raise
        else:
            if foreign_keys_enabled:
                await session.commit()
        finally:
            connection = await session.connection()
            if foreign_keys_enabled:
                await connection.execute(text("PRAGMA foreign_keys = ON"))
        return
    if database_type == "mysql":
        await connection.run_sync(migrate_mysql)
        return
