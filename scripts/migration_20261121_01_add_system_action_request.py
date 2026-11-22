from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261121_01_add_system_action_request_v1"


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()

    def _inspect_table(sync_connection) -> set[str] | None:
        inspector = inspect(sync_connection)
        if not inspector.has_table("audit_tool_detail"):
            return None
        return {str(column["name"]) for column in inspector.get_columns("audit_tool_detail")}

    columns = await connection.run_sync(_inspect_table)
    if columns is None:
        return

    if "decision_source" not in columns:
        await session.execute(text("ALTER TABLE audit_tool_detail ADD COLUMN decision_source VARCHAR(30) NOT NULL DEFAULT 'model_audit'"))
    if "action_request" not in columns:
        await session.execute(text("ALTER TABLE audit_tool_detail ADD COLUMN action_request JSON NULL"))
