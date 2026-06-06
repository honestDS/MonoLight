from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.prompt import (
    PromptCreate,
    PromptLibrary,
    PromptUpdate,
)


class CRUDPrompt(CRUDBase[PromptLibrary, PromptCreate, PromptUpdate]):
    async def get_by_name(self, db: AsyncSession, name: str) -> PromptLibrary | None:
        result = await db.execute(select(PromptLibrary).where(PromptLibrary.name == name))
        return result.scalars().first()


prompt_crud = CRUDPrompt(PromptLibrary)
