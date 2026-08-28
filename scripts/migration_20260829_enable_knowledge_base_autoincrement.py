from __future__ import annotations

import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t

MIGRATION_ID = "20260829_enable_knowledge_base_autoincrement_v1"

_TABLE_NAME = "knowledge_base"
_TEMP_TABLE_NAME = "knowledge_base__autoincrement_v1"

_CREATE_TABLE_PATTERN = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"knowledge_base\"|`knowledge_base`|\[knowledge_base\]|knowledge_base)\s*\(",
    re.IGNORECASE | re.DOTALL,
)
_ID_COLUMN_PATTERN = re.compile(
    r"(?im)^(?P<prefix>\s*(?:\"id\"|`id`|\[id\]|id)\s+INTEGER)(?:\s+NOT\s+NULL)?\s*,",
)
_TABLE_PRIMARY_KEY_PATTERN = re.compile(
    r",\s*(?:CONSTRAINT\s+(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|\S+)\s+)?PRIMARY\s+KEY\s*\(\s*(?:\"id\"|`id`|\[id\]|id)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _sqlite_table_sql(connection: Connection) -> str | None:
    return connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": _TABLE_NAME},
    ).scalar_one_or_none()


def _sqlite_uses_autoincrement(connection: Connection) -> bool:
    sql = _sqlite_table_sql(connection)
    return isinstance(sql, str) and "AUTOINCREMENT" in sql.upper()


def _build_autoincrement_table_sql(source_sql: str, connection: Connection) -> str:
    quoted_temp = _quote(connection, _TEMP_TABLE_NAME)
    rebuilt_sql, create_count = _CREATE_TABLE_PATTERN.subn(
        f"CREATE TABLE {quoted_temp} (",
        source_sql,
        count=1,
    )
    if create_count != 1:
        raise RuntimeError("unable to identify knowledge_base CREATE TABLE statement")

    rebuilt_sql, id_count = _ID_COLUMN_PATTERN.subn(
        lambda match: f"{match.group('prefix')} PRIMARY KEY AUTOINCREMENT,",
        rebuilt_sql,
        count=1,
    )
    if id_count != 1:
        raise RuntimeError(
            "knowledge_base id must be an INTEGER column for SQLite AUTOINCREMENT"
        )

    rebuilt_sql, primary_key_count = _TABLE_PRIMARY_KEY_PATTERN.subn(
        "",
        rebuilt_sql,
        count=1,
    )
    if primary_key_count != 1:
        raise RuntimeError("unable to identify knowledge_base primary key constraint")
    return rebuilt_sql


def _schema_objects(connection: Connection) -> list[str]:
    rows = connection.execute(
        text(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name = :table_name "
            "AND type IN ('index', 'trigger') "
            "AND sql IS NOT NULL "
            "ORDER BY type, name"
        ),
        {"table_name": _TABLE_NAME},
    ).all()
    return [str(row.sql) for row in rows if row.sql]


def _column_names(connection: Connection) -> list[str]:
    return [str(column["name"]) for column in inspect(connection).get_columns(_TABLE_NAME)]


def _rebuild_sqlite_knowledge_base(connection: Connection) -> None:
    if _TABLE_NAME not in inspect(connection).get_table_names():
        return
    if _sqlite_uses_autoincrement(connection):
        return

    source_sql = _sqlite_table_sql(connection)
    if not isinstance(source_sql, str) or not source_sql.strip():
        raise RuntimeError("knowledge_base CREATE TABLE statement is unavailable")

    columns = _column_names(connection)
    if "id" not in columns:
        raise RuntimeError("knowledge_base id column is missing")
    schema_objects = _schema_objects(connection)
    rebuilt_sql = _build_autoincrement_table_sql(source_sql, connection)

    connection.execute(text(f"DROP TABLE IF EXISTS {_quote(connection, _TEMP_TABLE_NAME)}"))
    connection.execute(text(rebuilt_sql))

    columns_sql = ", ".join(_quote(connection, name) for name in columns)
    connection.execute(
        text(
            f"INSERT INTO {_quote(connection, _TEMP_TABLE_NAME)} ({columns_sql}) "
            f"SELECT {columns_sql} FROM {_quote(connection, _TABLE_NAME)}"
        )
    )
    connection.execute(text(f"DROP TABLE {_quote(connection, _TABLE_NAME)}"))
    connection.execute(
        text(
            f"ALTER TABLE {_quote(connection, _TEMP_TABLE_NAME)} "
            f"RENAME TO {_quote(connection, _TABLE_NAME)}"
        )
    )
    for ddl in schema_objects:
        connection.execute(text(ddl))

    violations = connection.execute(text("PRAGMA foreign_key_check")).all()
    if violations:
        raise RuntimeError(
            f"knowledge_base autoincrement migration foreign key violations: {len(violations)}"
        )
    if not _sqlite_uses_autoincrement(connection):
        raise RuntimeError("knowledge_base AUTOINCREMENT migration did not take effect")


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
    if database_type == "mysql":
        return
    if database_type != "sqlite":
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))

    foreign_keys_enabled = bool(
        (await connection.execute(text("PRAGMA foreign_keys"))).scalar()
    )
    try:
        await connection.execute(text("PRAGMA foreign_keys = OFF"))
        if bool((await connection.execute(text("PRAGMA foreign_keys"))).scalar()):
            raise RuntimeError(
                "failed to disable SQLite foreign keys for knowledge_base rebuild"
            )
        await connection.run_sync(_rebuild_sqlite_knowledge_base)
    except Exception:
        await session.rollback()
        raise
    else:
        await session.commit()
    finally:
        connection = await session.connection()
        if foreign_keys_enabled:
            await connection.execute(text("PRAGMA foreign_keys = ON"))

