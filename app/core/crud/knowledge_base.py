from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
)


class CRUDKnowledgeBase(CRUDBase[KnowledgeBase, KnowledgeBaseCreate, KnowledgeBaseUpdate]):
    async def list_by_embedding_channel_id(self, db: AsyncSession, *, embedding_channel_id: int) -> list[KnowledgeBase]:
        result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.embedding_channel_id == embedding_channel_id))
        return list(result.scalars().all())


knowledge_base_crud = CRUDKnowledgeBase(KnowledgeBase)
