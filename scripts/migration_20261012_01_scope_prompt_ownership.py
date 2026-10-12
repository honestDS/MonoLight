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

MIGRATION_ID = "20261012_01_scope_prompt_ownership"

_PROMPT_REQUIRED_COLUMNS = {"id", "name", "content", "created_at"}


def _invalid() -> NoReturn:
    raise RuntimeError(t(ERR_MIGRATION_SCRIPT_INVALID, script_name=MIGRATION_ID))


def _schema(connection: Connection):
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    tables = {table_name: {str(column["name"]) for column in inspector.get_columns(table_name)} for table_name in ("prompt", "user", "profile") if table_name in table_names}
    prompt_indexes = inspector.get_indexes("prompt") if "prompt" in table_names else []
    return tables, prompt_indexes


async def _available_name(session: AsyncSession, uid: str, original_name: str, source_id: int) -> str:
    counter = 0
    while True:
        if counter == 0:
            name = original_name[:100]
        else:
            suffix = f" ({source_id})" if counter == 1 else f" ({source_id}-{counter})"
            name = f"{original_name[: 100 - len(suffix)]}{suffix}"
        result = await session.execute(
            text("SELECT 1 FROM prompt WHERE uid = :uid AND name = :name LIMIT 1"),
            {"uid": uid, "name": name},
        )
        if result.first() is None:
            return name
        counter += 1


async def migrate(session: AsyncSession) -> None:
    database_type = session.get_bind().dialect.name
    if database_type not in {"sqlite", "mysql"}:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))

    connection = await session.connection()
    tables, prompt_indexes = await connection.run_sync(_schema)
    prompt_columns = tables.get("prompt")
    if prompt_columns is None:
        return
    if not _PROMPT_REQUIRED_COLUMNS.issubset(prompt_columns):
        _invalid()

    if "uid" not in prompt_columns:
        await session.execute(text("ALTER TABLE prompt ADD COLUMN uid VARCHAR(50) NULL"))
    has_uid_index = False
    for index in prompt_indexes:
        index_columns = tuple(index.get("column_names") or ())
        if index.get("name") == "ix_prompt_uid" and index_columns != ("uid",):
            _invalid()
        if index_columns == ("uid",):
            has_uid_index = True
    if not has_uid_index:
        await session.execute(text("CREATE INDEX ix_prompt_uid ON prompt (uid)"))

    user_columns = tables.get("user", set())
    profile_columns = tables.get("profile", set())
    superuser_uid = None
    quoted_user_table = connection.dialect.identifier_preparer.quote("user")
    if {"id", "uid", "is_superuser"}.issubset(user_columns):
        result = await session.execute(
            text(f"SELECT uid FROM {quoted_user_table} WHERE is_superuser = :is_superuser AND uid IS NOT NULL ORDER BY id LIMIT 1"),
            {"is_superuser": True},
        )
        superuser_uid = result.scalar_one_or_none()

    result = await session.execute(text("SELECT id, name, content, created_at FROM prompt WHERE uid IS NULL ORDER BY id"))
    source_prompts = result.mappings().all()
    has_profile_references = {"uid", "prompt_id"}.issubset(profile_columns)

    for source in source_prompts:
        source_id = source["id"]
        original_name = source["name"]
        profile_uids = []
        if has_profile_references:
            profile_result = await session.execute(
                text("SELECT DISTINCT uid FROM profile WHERE prompt_id = :prompt_id AND uid IS NOT NULL ORDER BY uid"),
                {"prompt_id": source_id},
            )
            profile_uids = profile_result.scalars().all()

        owner_uid = superuser_uid if superuser_uid is not None else (profile_uids[0] if profile_uids else None)
        if owner_uid is None:
            continue

        owner_name = await _available_name(session, owner_uid, original_name, source_id)
        await session.execute(
            text("UPDATE prompt SET uid = :uid, name = :name WHERE id = :prompt_id AND uid IS NULL"),
            {"uid": owner_uid, "name": owner_name, "prompt_id": source_id},
        )

        for referenced_uid in profile_uids:
            if referenced_uid == owner_uid:
                continue
            copied_name = await _available_name(session, referenced_uid, original_name, source_id)
            insert_result = await session.execute(
                text("INSERT INTO prompt (uid, name, content, created_at) VALUES (:uid, :name, :content, :created_at)"),
                {
                    "uid": referenced_uid,
                    "name": copied_name,
                    "content": source["content"],
                    "created_at": source["created_at"],
                },
            )
            copied_prompt_id = insert_result.lastrowid
            if copied_prompt_id is None:
                _invalid()
            await session.execute(
                text("UPDATE profile SET prompt_id = :new_prompt_id WHERE prompt_id = :source_id AND uid = :uid"),
                {"source_id": source_id, "new_prompt_id": copied_prompt_id, "uid": referenced_uid},
            )
