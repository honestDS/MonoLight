import json
from typing import Any

from sqlalchemy import JSON, bindparam, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

# This filename intentionally sorts before migration_20260820_add_profile_recall_settings,
# whose ProfileConfig normalization would otherwise discard these legacy fields first.
MIGRATION_ID = "20261001_move_session_agent_settings"


def _inspect_columns(sync_connection, table_name: str) -> set[str] | None:
    inspector = inspect(sync_connection)
    if table_name not in inspector.get_table_names():
        return None
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


async def _table_columns(session: AsyncSession, table_name: str) -> set[str] | None:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: _inspect_columns(sync_connection, table_name))


def _decode_configs(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _legacy_setting(configs: dict[str, Any], name: str) -> bool | int | None:
    tool = configs.get("tool")
    if isinstance(tool, dict) and name in tool:
        value = tool[name]
    elif name in configs:
        value = configs[name]
    else:
        return None

    if name == "goal_mode":
        return value if type(value) is bool else None
    return value if type(value) is int and value >= 1 else None


def _clean_configs(configs: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    cleaned = dict(configs)
    changed = False

    tool = configs.get("tool")
    if isinstance(tool, dict):
        cleaned_tool = dict(tool)
        for name in ("goal_mode", "max_turns"):
            if name in cleaned_tool:
                del cleaned_tool[name]
                changed = True
        if cleaned_tool != tool:
            cleaned["tool"] = cleaned_tool

    for name in ("goal_mode", "max_turns"):
        if name in cleaned:
            del cleaned[name]
            changed = True
    return cleaned, changed


def _profile_state(row: Any) -> dict[str, Any]:
    configs = _decode_configs(row["configs"])
    values: dict[str, bool | int] = {}
    cleaned_configs: dict[str, Any] | None = None
    configs_changed = False
    if configs is not None:
        for name in ("goal_mode", "max_turns"):
            value = _legacy_setting(configs, name)
            if value is not None:
                values[name] = value
        cleaned_configs, configs_changed = _clean_configs(configs)

    return {
        "id": row["id"],
        "uid": row["uid"],
        "is_default": row["is_default"],
        "values": values,
        "configs": cleaned_configs,
        "configs_changed": configs_changed,
    }


def _is_default_profile(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1)


def _profile_indexes(profiles: list[dict[str, Any]]) -> tuple[dict[tuple[Any, Any], dict[str, Any]], dict[Any, dict[str, Any]]]:
    by_uid_and_id: dict[tuple[Any, Any], dict[str, Any]] = {}
    defaults_by_uid: dict[Any, dict[str, Any]] = {}
    for profile in profiles:
        key = (profile["uid"], profile["id"])
        by_uid_and_id[key] = profile
        if _is_default_profile(profile["is_default"]) and profile["uid"] not in defaults_by_uid:
            defaults_by_uid[profile["uid"]] = profile
    return by_uid_and_id, defaults_by_uid


def _select_profile(
    row: Any,
    by_uid_and_id: dict[tuple[Any, Any], dict[str, Any]],
    defaults_by_uid: dict[Any, dict[str, Any]],
) -> dict[str, Any] | None:
    uid = row["uid"]
    for column_name in ("profile_override_id", "profile_id"):
        profile = by_uid_and_id.get((uid, row[column_name]))
        if profile is not None:
            return profile
    return defaults_by_uid.get(uid)


async def _ensure_session_columns(session: AsyncSession, columns: set[str] | None) -> None:
    if columns is None:
        return
    if "goal_mode" not in columns:
        await session.execute(text("ALTER TABLE chat_session ADD COLUMN goal_mode BOOLEAN NOT NULL DEFAULT TRUE"))
    if "max_turns" not in columns:
        await session.execute(text("ALTER TABLE chat_session ADD COLUMN max_turns INTEGER NOT NULL DEFAULT 5"))


async def _read_profiles(session: AsyncSession, columns: set[str] | None) -> list[dict[str, Any]]:
    if columns is None:
        return []
    result = await session.execute(text("SELECT id, uid, is_default, configs FROM profile ORDER BY id"))
    return [_profile_state(row) for row in result.mappings().all()]


async def _move_session_settings(
    session: AsyncSession,
    columns: set[str] | None,
    by_uid_and_id: dict[tuple[Any, Any], dict[str, Any]],
    defaults_by_uid: dict[Any, dict[str, Any]],
) -> None:
    if columns is None or not {"session_id", "uid"} <= columns:
        return

    override_column = "profile_override_id" if "profile_override_id" in columns else "NULL"
    profile_column = "profile_id" if "profile_id" in columns else "NULL"
    result = await session.execute(text(f"SELECT session_id, uid, {override_column} AS profile_override_id, {profile_column} AS profile_id FROM chat_session"))
    for row in result.mappings().all():
        profile = _select_profile(row, by_uid_and_id, defaults_by_uid)
        if profile is None or not profile["values"]:
            continue

        assignments: list[str] = []
        parameters: dict[str, Any] = {"session_id": row["session_id"], "uid": row["uid"]}
        for name in ("goal_mode", "max_turns"):
            if name in profile["values"]:
                assignments.append(f"{name} = :{name}")
                parameters[name] = profile["values"][name]
        if assignments:
            await session.execute(
                text(f"UPDATE chat_session SET {', '.join(assignments)} WHERE session_id = :session_id AND uid = :uid"),
                parameters,
            )


async def _clean_profiles(session: AsyncSession, profiles: list[dict[str, Any]]) -> None:
    update = text("UPDATE profile SET configs = :configs WHERE id = :profile_id").bindparams(bindparam("configs", type_=JSON()))
    for profile in profiles:
        if not profile["configs_changed"]:
            continue
        await session.execute(
            update,
            {
                "profile_id": profile["id"],
                "configs": profile["configs"],
            },
        )


async def migrate(session: AsyncSession) -> None:
    chat_session_columns = await _table_columns(session, "chat_session")
    profile_columns = await _table_columns(session, "profile")

    await _ensure_session_columns(session, chat_session_columns)
    profiles = await _read_profiles(session, profile_columns)
    by_uid_and_id, defaults_by_uid = _profile_indexes(profiles)
    await _move_session_settings(session, chat_session_columns, by_uid_and_id, defaults_by_uid)
    await _clean_profiles(session, profiles)
