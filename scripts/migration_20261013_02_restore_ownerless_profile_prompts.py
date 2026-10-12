from __future__ import annotations

from typing import NoReturn

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_DATABASE_TYPE_UNSUPPORTED,
    ERR_MIGRATION_SCRIPT_INVALID,
)
from app.core.i18n import t

MIGRATION_ID = "20261013_02_restore_ownerless_profile_prompts"

_PROMPT_REQUIRED_COLUMNS = {"id", "uid", "name", "content", "created_at"}
_PROFILE_REQUIRED_COLUMNS = {"uid", "prompt_id"}


def _invalid() -> NoReturn:
    raise RuntimeError(t(ERR_MIGRATION_SCRIPT_INVALID, script_name=MIGRATION_ID))


def _schema(connection: Connection) -> dict[str, set[str]]:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    return {table_name: {str(column["name"]) for column in inspector.get_columns(table_name)} for table_name in ("prompt", "profile") if table_name in table_names}


async def _available_name(session: AsyncSession, original_name: str, source_id: int) -> str:
    counter = 0
    while True:
        if counter == 0:
            name = original_name[:100]
        else:
            suffix = f" ({source_id})" if counter == 1 else f" ({source_id}-{counter})"
            name = f"{original_name[: 100 - len(suffix)]}{suffix}"
        result = await session.execute(
            text("SELECT 1 FROM prompt WHERE uid IS NULL AND name = :name LIMIT 1"),
            {"name": name},
        )
        if result.first() is None:
            return name
        counter += 1


async def migrate(session: AsyncSession) -> None:
    database_type = session.get_bind().dialect.name
    if database_type not in {"sqlite", "mysql"}:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))

    connection = await session.connection()
    tables = await connection.run_sync(_schema)
    prompt_columns = tables.get("prompt")
    profile_columns = tables.get("profile")
    if prompt_columns is None or profile_columns is None:
        return
    if not _PROMPT_REQUIRED_COLUMNS.issubset(prompt_columns) or not _PROFILE_REQUIRED_COLUMNS.issubset(profile_columns):
        _invalid()

    result = await session.execute(
        text(
            """
            SELECT DISTINCT prompt.id, prompt.name, prompt.content, prompt.created_at
            FROM prompt
            JOIN profile ON profile.prompt_id = prompt.id
            WHERE profile.uid IS NULL AND prompt.uid IS NOT NULL
            ORDER BY prompt.id ASC
            """
        )
    )
    source_prompts = result.mappings().all()

    for source in source_prompts:
        source_id = source["id"]
        copied_name = await _available_name(session, source["name"], source_id)
        insert_result = await session.execute(
            text("INSERT INTO prompt (uid, name, content, created_at) VALUES (:uid, :name, :content, :created_at)"),
            {
                "uid": None,
                "name": copied_name,
                "content": source["content"],
                "created_at": source["created_at"],
            },
        )
        copied_prompt_id = insert_result.lastrowid
        if copied_prompt_id is None:
            _invalid()

        await session.execute(
            text("UPDATE profile SET prompt_id = :new_prompt_id WHERE uid IS NULL AND prompt_id = :source_id"),
            {"source_id": source_id, "new_prompt_id": copied_prompt_id},
        )
