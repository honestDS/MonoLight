from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session_todo import SessionTodoPlan

MIGRATION_ID = "20260917_add_session_todo_plan"


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(lambda sync_connection: SessionTodoPlan.__table__.create(sync_connection, checkfirst=True))
