from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_DATABASE_TYPE_UNSUPPORTED,
    ERR_MIGRATION_SCRIPT_INVALID,
    ERR_PROMPT_NAME_EXISTS,
)
from app.core.i18n import t

MIGRATION_ID = "20261015_01_add_prompt_owner_name_unique"

_TABLE_NAME = "prompt"
_UNIQUE_NAME = "uq_prompt_uid_name"
_UNIQUE_COLUMNS = ("uid", "name")


def _invalid() -> None:
    raise RuntimeError(t(ERR_MIGRATION_SCRIPT_INVALID, script_name=MIGRATION_ID))


def _columns(item: dict) -> tuple[str, ...]:
    return tuple(item.get("column_names") or ())


def _migrate_sync(connection: Connection) -> None:
    inspector = inspect(connection)
    if _TABLE_NAME not in set(inspector.get_table_names()):
        return

    columns = {str(column["name"]) for column in inspector.get_columns(_TABLE_NAME)}
    if not columns.issuperset(_UNIQUE_COLUMNS):
        _invalid()

    indexes = inspector.get_indexes(_TABLE_NAME)
    unique_constraints = inspector.get_unique_constraints(_TABLE_NAME)
    named_indexes = [index for index in indexes if index.get("name") == _UNIQUE_NAME]
    named_constraints = [constraint for constraint in unique_constraints if constraint.get("name") == _UNIQUE_NAME]

    if any(not index.get("unique") or _columns(index) != _UNIQUE_COLUMNS for index in named_indexes):
        _invalid()
    if any(_columns(constraint) != _UNIQUE_COLUMNS for constraint in named_constraints):
        _invalid()
    if named_indexes or named_constraints:
        return

    duplicate = connection.execute(text("SELECT 1 FROM prompt WHERE uid IS NOT NULL AND name IS NOT NULL GROUP BY uid, name HAVING COUNT(*) > 1 LIMIT 1")).first()
    if duplicate is not None:
        raise RuntimeError(t(ERR_PROMPT_NAME_EXISTS))

    connection.execute(text("CREATE UNIQUE INDEX uq_prompt_uid_name ON prompt (uid, name)"))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
    if database_type not in {"mysql", "sqlite"}:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))
    await connection.run_sync(_migrate_sync)
