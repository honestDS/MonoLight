from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.models.profile import Profile
from app.models.message import Message
from app.models.prompt import PromptLibrary
from app.providers.llm.client import LLMClient
from app.core.context import ContextManager
from fastapi import HTTPException

class ChatDispatcher:
    @staticmethod
    async def dispatch(db: AsyncSession, message: str, session_id: str = 'default'):
        # 1. 获取激活的 Profile，同时加载 provider 和 prompt
        stmt = select(Profile).where(Profile.is_active == True).options(
            selectinload(Profile.provider),
            selectinload(Profile.prompt)
        )
        profile = (await db.execute(stmt)).scalars().first()
        if not profile: raise HTTPException(status_code=400, detail='No active profile')

        # 2. 获取处理后的历史消息
        messages = await ContextManager.get_messages(db, session_id, profile, message)

        # 3. 提示词注入逻辑 (如果 Profile 关联了 PromptLibrary)
        if profile.prompt and profile.prompt.content:
            messages.insert(0, {'role': 'system', 'content': profile.prompt.content})

        # 4. 容错处理：如果是虚拟 Provider (-1)
        if profile.provider_id == -1:
            # 这里实现虚模型的响应，或抛出配置未完成异常
            # 为了业务闭环，此处返回一个模拟响应或请求错误
            raise HTTPException(status_code=400, detail='Profile is not bound to a real provider (provider_id=-1)')

        if not profile.provider:
            raise HTTPException(status_code=400, detail='Active profile has no valid provider')

        # 5. 请求 LLM
        response = await LLMClient.generate(profile, messages)
        ai_content = response['choices'][0]['message']['content']

        # 6. 持久化
        db.add_all([
            Message(session_id=session_id, role='user', content=message, profile_id=profile.id),
            Message(session_id=session_id, role='assistant', content=ai_content, profile_id=profile.id)
        ])
        await db.commit()
        return response
