from sqlalchemy import JSON, Boolean, Column, Integer, MetaData, Table, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261016_01_split_message_platform_stream_mode"
_BATCH_SIZE = 500

_MESSAGE_PLATFORM = Table(
    "message_platform",
    MetaData(),
    Column("id", Integer),
    Column("config", JSON),
    Column("use_stream_dispatch", Boolean),
)


async def _table_columns(session: AsyncSession, table_name: str) -> set[str] | None:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str] | None:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return None
        return {str(column["name"]) for column in inspector.get_columns(table_name)}

    return await connection.run_sync(inspect_table)


async def migrate(session: AsyncSession) -> None:
    columns = await _table_columns(session, "message_platform")
    required_columns = {"id", "config", "use_stream_dispatch"}
    if columns is None or not required_columns.issubset(columns):
        return

    last_id: int | None = None
    while True:
        statement = (
            select(
                _MESSAGE_PLATFORM.c.id,
                _MESSAGE_PLATFORM.c.config,
                _MESSAGE_PLATFORM.c.use_stream_dispatch,
            )
            .order_by(_MESSAGE_PLATFORM.c.id.asc())
            .limit(_BATCH_SIZE)
        )
        if last_id is not None:
            statement = statement.where(_MESSAGE_PLATFORM.c.id > last_id)

        rows = (await session.execute(statement)).all()
        if not rows:
            return

        for row in rows:
            if row.config is None:
                config = {}
            elif isinstance(row.config, dict):
                config = dict(row.config)
            else:
                continue

            if "stream_llm_request" in config:
                continue

            config["stream_llm_request"] = bool(row.use_stream_dispatch)
            await session.execute(update(_MESSAGE_PLATFORM).where(_MESSAGE_PLATFORM.c.id == row.id).values(config=config))

        last_id = rows[-1].id
