from sqlalchemy.ext.asyncio import AsyncSession

from app.models.worker_lease import WorkerLease

MIGRATION_ID = "20260711_add_worker_lease"


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(
        lambda sync_connection: WorkerLease.__table__.create(
            sync_connection,
            checkfirst=True,
        )
    )
