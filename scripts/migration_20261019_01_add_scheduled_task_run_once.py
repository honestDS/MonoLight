from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261019_01_add_scheduled_task_run_once"

SCHEDULED_TASK_TABLE = "scheduled_task"
RUN_ONCE_COLUMN = "run_once"


def _scheduled_task_columns(sync_connection) -> set[str] | None:
    inspector = inspect(sync_connection)
    if SCHEDULED_TASK_TABLE not in set(inspector.get_table_names()):
        return None
    return {str(column["name"]) for column in inspector.get_columns(SCHEDULED_TASK_TABLE)}


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    scheduled_task_columns = await connection.run_sync(_scheduled_task_columns)
    if scheduled_task_columns is None or RUN_ONCE_COLUMN in scheduled_task_columns:
        return

    await session.execute(text(f"ALTER TABLE {SCHEDULED_TASK_TABLE} ADD COLUMN {RUN_ONCE_COLUMN} BOOLEAN NOT NULL DEFAULT 0"))
