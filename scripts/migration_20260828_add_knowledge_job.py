from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeJob

MIGRATION_ID = "20260828_add_knowledge_job_v1"


def _ensure_knowledge_job_table(connection) -> None:
    KnowledgeJob.__table__.create(bind=connection, checkfirst=True)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_knowledge_job_table)
