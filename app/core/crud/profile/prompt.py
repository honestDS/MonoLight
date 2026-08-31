from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.prompt import (
    PromptCreate,
    PromptLibrary,
    PromptUpdate,
)


class CRUDPrompt(CRUDBase[PromptLibrary, PromptCreate, PromptUpdate]):
    async def get_by_name(self, db: AsyncSession, name: str, uid: str | None = None) -> PromptLibrary | None:
        result = await db.execute(select(PromptLibrary).where(PromptLibrary.name == name).where(PromptLibrary.uid == uid))
        return result.scalars().first()

    async def get_visible(self, db: AsyncSession, prompt_id: int, uid: str | None = None) -> PromptLibrary | None:
        result = await db.execute(select(PromptLibrary).where(PromptLibrary.id == prompt_id).where((PromptLibrary.uid == uid) | (PromptLibrary.uid.is_(None))))
        return result.scalars().first()

    async def get_multi_visible(self, db: AsyncSession, *, skip: int = 0, limit: int = 100, uid: str | None = None) -> list[PromptLibrary]:
        stmt = select(PromptLibrary).where((PromptLibrary.uid == uid) | (PromptLibrary.uid.is_(None))).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def count_visible(self, db: AsyncSession, uid: str | None = None) -> int:
        result = await db.execute(select(func.count()).select_from(PromptLibrary).where((PromptLibrary.uid == uid) | (PromptLibrary.uid.is_(None))))
        return result.scalar() or 0


prompt_crud = CRUDPrompt(PromptLibrary)
