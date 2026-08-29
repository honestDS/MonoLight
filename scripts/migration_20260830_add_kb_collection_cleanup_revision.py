from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t
from app.models.knowledge_base import install_knowledge_base_collection_owner_triggers

MIGRATION_ID = "20260830_add_kb_collection_cleanup_revision_v1"

_TABLE_NAME = "knowledge_base_collection_owner"
_COLUMN_NAME = "cleanup_revision"


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _add_cleanup_revision(connection: Connection) -> None:
    inspector = inspect(connection)
    if _TABLE_NAME not in inspector.get_table_names():
        return
    columns = {str(column["name"]) for column in inspector.get_columns(_TABLE_NAME)}
    if _COLUMN_NAME in columns:
        return

    connection.execute(text(f"ALTER TABLE {_quote(connection, _TABLE_NAME)} ADD COLUMN {_quote(connection, _COLUMN_NAME)} INTEGER NOT NULL DEFAULT 0"))


def _upgrade_cleanup_revision(connection: Connection) -> None:
    if _TABLE_NAME not in inspect(connection).get_table_names():
        return
    _add_cleanup_revision(connection)
    install_knowledge_base_collection_owner_triggers(connection)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
    if database_type not in {"sqlite", "mysql"}:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))
    await connection.run_sync(_upgrade_cleanup_revision)
    await session.commit()
