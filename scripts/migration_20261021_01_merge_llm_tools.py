from sqlalchemy import JSON, Column, Integer, MetaData, Table, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261021_01_merge_llm_tools"
_BATCH_SIZE = 500

_PROFILE = Table(
    "profile",
    MetaData(),
    Column("id", Integer),
    Column("configs", JSON),
)

_TOOL_RENAMES = {
    "write_file": "file_tool",
    "firecrawl_search": "web_tool",
    "firecrawl_scrape": "web_tool",
    "list_background_tasks": "manage_background_tasks",
    "cancel_background_task": "manage_background_tasks",
}


async def _table_columns(session: AsyncSession, table_name: str) -> set[str] | None:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str] | None:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return None
        return {str(column["name"]) for column in inspector.get_columns(table_name)}

    return await connection.run_sync(inspect_table)


def _migrate_config(config: dict) -> tuple[dict, bool]:
    updated = dict(config)
    changed = False

    enabled_tools = config.get("enabled_tools")
    if isinstance(enabled_tools, list):
        normalized_tools = []
        seen_tools = set()
        for tool_name in enabled_tools:
            if not isinstance(tool_name, str):
                normalized_tools.append(tool_name)
                continue

            normalized_name = _TOOL_RENAMES.get(tool_name, tool_name)
            if normalized_name not in seen_tools:
                seen_tools.add(normalized_name)
                normalized_tools.append(normalized_name)

        if normalized_tools != enabled_tools:
            updated["enabled_tools"] = normalized_tools
            changed = True

    for key in ("tool", "configs"):
        nested_config = config.get(key)
        if isinstance(nested_config, dict):
            migrated_nested, nested_changed = _migrate_config(nested_config)
            if nested_changed:
                updated[key] = migrated_nested
                changed = True

    return (updated if changed else config), changed


async def migrate(session: AsyncSession) -> None:
    columns = await _table_columns(session, "profile")
    if columns is None or not {"id", "configs"}.issubset(columns):
        return

    last_id: int | None = None
    while True:
        statement = select(_PROFILE.c.id, _PROFILE.c.configs).order_by(_PROFILE.c.id.asc()).limit(_BATCH_SIZE)
        if last_id is not None:
            statement = statement.where(_PROFILE.c.id > last_id)

        rows = (await session.execute(statement)).all()
        if not rows:
            return

        for row in rows:
            if not isinstance(row.configs, dict):
                continue

            configs, changed = _migrate_config(row.configs)
            if changed:
                await session.execute(update(_PROFILE).where(_PROFILE.c.id == row.id).values(configs=configs))

        last_id = rows[-1].id
