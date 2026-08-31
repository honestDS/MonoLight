from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_base import KnowledgeBaseEmbeddingDelta

MIGRATION_ID = "20260831_add_kb_embedding_delta_v1"


def _ensure_knowledge_base_embedding_delta_table(connection) -> None:
    KnowledgeBaseEmbeddingDelta.__table__.create(bind=connection, checkfirst=True)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_knowledge_base_embedding_delta_table)
