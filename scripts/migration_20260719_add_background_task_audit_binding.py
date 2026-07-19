from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260719_add_background_task_audit_binding"


async def _existing_columns(session: AsyncSession, table_name: str) -> set[str]:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: {str(item["name"]) for item in inspect(sync_connection).get_columns(table_name)})


async def _existing_index_names(session: AsyncSession, table_name: str) -> set[str]:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: {str(item["name"]) for item in inspect(sync_connection).get_indexes(table_name)})


async def _has_unique_column_constraint(session: AsyncSession, table_name: str, column_name: str) -> bool:
    connection = await session.connection()

    def inspect_unique(sync_connection) -> bool:
        inspector = inspect(sync_connection)
        for index in inspector.get_indexes(table_name):
            if index.get("unique") and list(index.get("column_names") or []) == [column_name]:
                return True
        for constraint in inspector.get_unique_constraints(table_name):
            if list(constraint.get("column_names") or []) == [column_name]:
                return True
        return False

    return await connection.run_sync(inspect_unique)


async def migrate(session: AsyncSession) -> None:
    columns = await _existing_columns(session, "background_task")
    if "audit_record_id" not in columns:
        await session.execute(text("ALTER TABLE background_task ADD COLUMN audit_record_id INTEGER"))
    if "audit_execution_record_id" not in columns:
        await session.execute(text("ALTER TABLE background_task ADD COLUMN audit_execution_record_id INTEGER"))

    indexes = await _existing_index_names(session, "background_task")
    if "ix_background_task_audit_record_id" not in indexes:
        await session.execute(text("CREATE INDEX ix_background_task_audit_record_id ON background_task (audit_record_id)"))
    if not await _has_unique_column_constraint(session, "background_task", "audit_execution_record_id"):
        await session.execute(text("CREATE UNIQUE INDEX uq_background_task_audit_execution_record_id ON background_task (audit_execution_record_id)"))
