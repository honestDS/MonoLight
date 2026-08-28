from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import ManagedKnowledgeItem, ManagedKnowledgeRevision

MIGRATION_ID = "20260827_add_managed_knowledge_records_v1"


def _ensure_managed_knowledge_tables(connection) -> None:
    ManagedKnowledgeItem.__table__.create(bind=connection, checkfirst=True)
    ManagedKnowledgeRevision.__table__.create(bind=connection, checkfirst=True)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_managed_knowledge_tables)
