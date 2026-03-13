from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.models.profile import Profile
from app.providers.llm.client import LLMClient
from fastapi import HTTPException

class ChatDispatcher:
    @staticmethod
    async def dispatch(db: AsyncSession, message: str):
        stmt = select(Profile).where(Profile.is_active == True).options(selectinload(Profile.provider))
        result = await db.execute(stmt)
        profile = result.scalars().first()
        
        if not profile:
            raise HTTPException(status_code=404, detail='No active profile found')
        
        if not profile.provider:
            raise HTTPException(status_code=400, detail=f'Profile {profile.name} has no associated provider')
        
        return await LLMClient.generate(profile, message)