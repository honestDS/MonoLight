from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261021_02_migrate_legacy_web_background_tasks"

_LEGACY_OPERATIONS = {
    "firecrawl_search": "search",
    "firecrawl_scrape": "scrape",
}
_ACTIVE_STATUSES = ("pending", "running", "PENDING", "RUNNING")


def _table_columns(sync_session):
    inspector = inspect(sync_session.connection())
    if not inspector.has_table("background_task"):
        return None
    return {column["name"] for column in inspector.get_columns("background_task")}


async def migrate(session: AsyncSession) -> None:
    columns = await session.run_sync(_table_columns)
    if columns is None or not {"id", "tool_name", "status", "arguments"}.issubset(columns):
        return

    table = Table(
        "background_task",
        MetaData(),
        Column("id", Integer),
        Column("tool_name", String),
        Column("status", String),
        Column("arguments", JSON),
    )
    last_id = None

    while True:
        statement = (
            select(table.c.id, table.c.tool_name, table.c.arguments)
            .where(
                table.c.tool_name.in_(tuple(_LEGACY_OPERATIONS)),
                table.c.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(table.c.id)
            .limit(500)
        )
        if last_id is not None:
            statement = statement.where(table.c.id > last_id)

        rows = (await session.execute(statement)).all()
        if not rows:
            break

        for row in rows:
            if not isinstance(row.arguments, dict):
                continue
            await session.execute(
                update(table)
                .where(
                    table.c.id == row.id,
                    table.c.tool_name == row.tool_name,
                    table.c.status.in_(_ACTIVE_STATUSES),
                )
                .values(
                    tool_name="web_tool",
                    arguments={
                        **row.arguments,
                        "operation": _LEGACY_OPERATIONS[row.tool_name],
                    },
                )
            )

        last_id = rows[-1].id
