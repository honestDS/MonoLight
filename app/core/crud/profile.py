from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.crud.base import CRUDBase
from app.models.profile import (
    Profile,
    ProfileCreate,
    ProfileUpdate,
)


class CRUDProfile(CRUDBase[Profile, ProfileCreate, ProfileUpdate]):
    async def get_with_relations(self, db: AsyncSession, id: int) -> Profile | None:
        stmt = select(Profile).where(Profile.id == id).options(selectinload(Profile.provider))
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_by_name(self, db: AsyncSession, name: str) -> Profile | None:
        stmt = select(Profile).where(Profile.name == name)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_multi(self, db: AsyncSession, *, skip: int = 0, limit: int = 100) -> list[Profile]:
        stmt = select(Profile).options(selectinload(Profile.provider)).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def get_active(self, db: AsyncSession) -> Profile | None:
        stmt = select(Profile).where(Profile.is_active).options(selectinload(Profile.provider), selectinload(Profile.prompt))
        result = await db.execute(stmt)
        return result.scalars().first()


profile_crud = CRUDProfile(Profile)
