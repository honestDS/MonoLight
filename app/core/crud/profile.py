from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.core.crud.base import CRUDBase
from app.models.profile import Profile, ProfileCreate, ProfileUpdate

class CRUDProfile(CRUDBase[Profile, ProfileCreate, ProfileUpdate]):
    async def get_active(self, db: AsyncSession) -> Optional[Profile]:
        stmt = (
            select(Profile)
            .where(Profile.is_active)
            .options(selectinload(Profile.provider), selectinload(Profile.prompt))
        )
        result = await db.execute(stmt)
        return result.scalars().first()

profile_crud = CRUDProfile(Profile)
