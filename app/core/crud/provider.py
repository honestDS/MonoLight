from typing import Optional
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.crud.base import CRUDBase
from app.models.provider import (
    ModelProvider,
    ProviderCreate,
    ProviderUpdate,
)


class CRUDProvider(CRUDBase[ModelProvider, ProviderCreate, ProviderUpdate]):
    async def get_by_name(self, db: AsyncSession, name: str) -> Optional[ModelProvider]:
        result = await db.execute(
            select(ModelProvider).where(ModelProvider.name == name)
        )
        return result.scalars().first()


provider_crud = CRUDProvider(ModelProvider)
