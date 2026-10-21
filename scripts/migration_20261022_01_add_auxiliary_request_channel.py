from copy import deepcopy
from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, Table, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261022_01_add_auxiliary_request_channel"
_BATCH_SIZE = 500

_PROFILE = Table(
    "profile",
    MetaData(),
    Column("id", Integer),
    Column("configs", JSON),
)


async def _table_columns(session: AsyncSession, table_name: str) -> set[str] | None:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str] | None:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return None
        return {str(column["name"]) for column in inspector.get_columns(table_name)}

    return await connection.run_sync(inspect_table)


def _iter_config_nodes(configs: dict[str, Any]):
    pending = [(configs, False)]
    while pending:
        current, under_channel = pending.pop()
        yield current, under_channel

        for key in ("configs", "channel"):
            nested = current.get(key)
            if isinstance(nested, dict):
                pending.append((nested, under_channel or key == "channel"))


def _migrate_configs(configs: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    nodes = list(_iter_config_nodes(configs))
    if any("auxiliary_channel" in node for node, _ in nodes):
        return configs, False

    updated = deepcopy(configs)
    updated_nodes = list(_iter_config_nodes(updated))
    source_node = next(
        (node for node, under_channel in updated_nodes if under_channel and "context_summary_channel" in node),
        None,
    )
    if source_node is None:
        source_node = next(
            (node for node, _ in updated_nodes if "context_summary_channel" in node),
            None,
        )
    if source_node is None:
        return configs, False

    source_node["auxiliary_channel"] = deepcopy(source_node["context_summary_channel"])
    return updated, True


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

            configs, changed = _migrate_configs(row.configs)
            if changed:
                await session.execute(update(_PROFILE).where(_PROFILE.c.id == row.id).values(configs=configs))

        last_id = rows[-1].id
