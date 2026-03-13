from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.profile import Profile
from app.models.provider import ModelProvider
from fastapi import HTTPException
import logging

logger = logging.getLogger(__name__)

class ChatDispatcher:
    @staticmethod
    async def get_active_config(db: AsyncSession):
        result = await db.execute(
            select(Profile).where(Profile.is_active == True)
        )
        profile = result.scalars().first()
        if not profile:
            return None
        return profile

    @staticmethod
    async def dispatch(db: AsyncSession, messages: list):
        profile = await ChatDispatcher.get_active_config(db)
        if not profile:
            raise HTTPException(status_code=404, detail='No active agent profile found')

        config = {
            'model': profile.model_id,
            'temperature': profile.temperature,
            'top_p': profile.top_p,
            'max_tokens': profile.max_tokens,
            'stream': profile.stream,
            'messages': messages
        }
        return config, profile

class Dispatcher:
    def __init__(self):
        self.middlewares = []

    async def dispatch(self, message: str):
        logger.info(f'General dispatcher: {message}')
        return True