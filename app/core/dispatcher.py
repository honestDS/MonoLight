from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.models.profile import Profile
from app.models.message import Message
from app.providers.llm.client import LLMClient
from app.core.context import ContextManager
from fastapi import HTTPException

class ChatDispatcher:
    @staticmethod
    async def dispatch(db: AsyncSession, message: str, session_id: str = 'default'):
        stmt = select(Profile).where(Profile.is_active == True).options(selectinload(Profile.provider))
        profile = (await db.execute(stmt)).scalars().first()
        if not profile or not profile.provider: raise HTTPException(status_code=400, detail='Invalid Profile')

        # 获取处理后的上下文
        messages = await ContextManager.get_messages(db, session_id, profile, message)

        # 请求 LLM
        response = await LLMClient.generate(profile, messages)

        # 提取回复内容
        ai_content = response['choices'][0]['message']['content']

        # 持久化本次对话
        db.add_all([
            Message(session_id=session_id, role='user', content=message, profile_id=profile.id),
            Message(session_id=session_id, role='assistant', content=ai_content, profile_id=profile.id)
        ])
        await db.commit()
        return response