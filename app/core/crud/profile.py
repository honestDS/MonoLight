from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.base import CRUDBase
from app.models.profile import (
    Profile,
    ProfileCreate,
    ProfileUpdate,
)


class CRUDProfile(CRUDBase[Profile, ProfileCreate, ProfileUpdate]):
    async def get_by_uid(self, db: AsyncSession, uid: str | None) -> Profile | None:
        stmt = select(Profile).where(Profile.uid == uid)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_with_relations(self, db: AsyncSession, id: int) -> Profile | None:
        stmt = select(Profile).where(Profile.id == id)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_by_name(self, db: AsyncSession, name: str, uid: str | None = None) -> Profile | None:
        stmt = select(Profile).where(Profile.name == name).where(Profile.uid == uid)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def get_multi(self, db: AsyncSession, *, skip: int = 0, limit: int = 100, uid: str | None = None) -> list[Profile]:
        stmt = select(Profile).where(Profile.uid == uid).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_multi_all(self, db: AsyncSession, *, skip: int = 0, limit: int = 100) -> list[Profile]:
        stmt = select(Profile).offset(skip).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def count(self, db: AsyncSession, uid: str | None = None) -> int:
        result = await db.execute(select(func.count()).select_from(Profile).where(Profile.uid == uid))
        return result.scalar()

    async def count_all(self, db: AsyncSession) -> int:
        result = await db.execute(select(func.count()).select_from(Profile))
        return result.scalar()

    async def get_active(self, db: AsyncSession, uid: str | None = None) -> Profile | None:
        stmt = select(Profile).where(Profile.uid == uid).where(Profile.is_active)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def deactivate_by_uid(self, db: AsyncSession, uid: str | None) -> None:
        profiles = await self.get_multi(db, uid=uid)
        for profile in profiles:
            profile.is_active = False
            db.add(profile)

    async def get_multi_by_prompt_id(self, db: AsyncSession, prompt_id: int) -> list[Profile]:
        result = await db.execute(select(Profile).where(Profile.prompt_id == prompt_id))
        return list(result.scalars().all())

    async def reassign_prompt(self, db: AsyncSession, *, source_prompt_id: int, target_prompt_id: int) -> int:
        profiles = await self.get_multi_by_prompt_id(db, source_prompt_id)
        for profile in profiles:
            profile.prompt_id = target_prompt_id
            db.add(profile)
        return len(profiles)


profile_crud = CRUDProfile(Profile)
