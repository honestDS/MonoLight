from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.profile import Profile
from fastapi import HTTPException
import logging

logger = logging.getLogger(__name__)

class ChatDispatcher:
    @staticmethod
    async def dispatch(db: AsyncSession, message: str):
        result = await db.execute(select(Profile).where(Profile.is_active == True))
        profile = result.scalars().first()
        if not profile:
            raise HTTPException(status_code=404, detail='No active profile found')
        return profile