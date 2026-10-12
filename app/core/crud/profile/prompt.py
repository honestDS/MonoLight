from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.models.prompt import (
    PromptCreate,
    PromptLibrary,
    PromptUpdate,
)


class CRUDPrompt(CRUDBase[PromptLibrary, PromptCreate, PromptUpdate]):
    async def get(
        self,
        db: AsyncSession,
        id: int,
        *,
        lock_for_reference_write: bool = False,
    ) -> PromptLibrary | None:
        if not lock_for_reference_write:
            return await super().get(db, id)
        await db.execute(update(PromptLibrary).where(PromptLibrary.id == id).values(name=PromptLibrary.name).execution_options(synchronize_session=False))
        result = await db.execute(select(PromptLibrary).where(PromptLibrary.id == id).with_for_update().execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_by_name(self, db: AsyncSession, name: str, uid: str | None = None) -> PromptLibrary | None:
        result = await db.execute(select(PromptLibrary).where(PromptLibrary.name == name).where(PromptLibrary.uid == uid))
        return result.scalars().first()

    async def get_visible(
        self,
        db: AsyncSession,
        prompt_id: int,
        uid: str | None = None,
        *,
        lock_for_reference_write: bool = False,
    ) -> PromptLibrary | None:
        if uid is None:
            return None
        if lock_for_reference_write:
            prompt = await self.get(db, prompt_id, lock_for_reference_write=True)
            if prompt is None or prompt.uid != uid:
                return None
            return prompt
        result = await db.execute(select(PromptLibrary).where(PromptLibrary.id == prompt_id).where(PromptLibrary.uid == uid))
        return result.scalars().first()

    async def get_multi_visible(self, db: AsyncSession, *, skip: int = 0, limit: int = 100, uid: str | None = None) -> list[PromptLibrary]:
        if uid is None:
            return []
        stmt = select(PromptLibrary).where(PromptLibrary.uid == uid).order_by(PromptLibrary.id.desc()).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def count_visible(self, db: AsyncSession, uid: str | None = None) -> int:
        if uid is None:
            return 0
        result = await db.execute(select(func.count()).select_from(PromptLibrary).where(PromptLibrary.uid == uid))
        return result.scalar() or 0


prompt_crud = CRUDPrompt(PromptLibrary)
