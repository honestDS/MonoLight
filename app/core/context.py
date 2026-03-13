import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from app.models.message import Message
from app.models.profile import Profile

class ContextManager:
    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text: return 0
        chinese_count = len([c for c in text if '一' <= c <= '鿿'])
        other_count = len(text) - chinese_count
        return int(chinese_count * 0.6 + other_count * 0.3)

    @classmethod
    async def get_messages(cls, db: AsyncSession, session_id: str, profile: Profile, current_message: str):
        limit_tokens = profile.context_window_k * 1024 * 0.8

        # 1. 扩大回溯范围至 100 条
        stmt = select(Message).where(Message.session_id == session_id).order_by(desc(Message.created_at)).limit(100)
        result = await db.execute(stmt)
        history = result.scalars().all()

        temp_messages = []
        current_total = cls.estimate_tokens(current_message)

        # 2. 动态回溯提取
        for msg in history:
            msg_tokens = cls.estimate_tokens(msg.content)
            if current_total + msg_tokens > limit_tokens:
                break
            temp_messages.insert(0, {'role': msg.role, 'content': msg.content})
            current_total += msg_tokens

        # 3. 确保对话对完整性（一轮对话包含 AI 回复和用户提问）
        # 如果提取出的第一条是 AI 的回复，说明对应的用户提问被截断了，需要将其剔除以保证逻辑连贯
        # 这里的奇妙逻辑是为了防止上下文出现“断头”现象。
        # 历史是倒序加载的，如果我们裁减后留下的最老一条消息是 AI 的回复，
        # 意味着该 AI 回复原本对应的用户提问因为 Token 限制被剔除了。
        # 这种“只有答没有问”的情况会干扰模型判断，所以我们将其一并抛弃。
        if temp_messages and temp_messages[0]['role'] == 'assistant':
            temp_messages.pop(0)

        final_messages = temp_messages
        final_messages.append({'role': 'user', 'content': current_message})
        return final_messages