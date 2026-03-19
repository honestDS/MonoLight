import json
import os

from dotenv import load_dotenv
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.models.profile import Profile

load_dotenv()


class ContextManager:
    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        chinese_count = len([c for c in text if "一" <= c <= "鿿"])
        other_count = len(text) - chinese_count
        c_coeff = float(os.getenv("TOKEN_COEFF_CHINESE", 0.6))
        o_coeff = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
        return int(chinese_count * c_coeff + other_count * o_coeff)

    @classmethod
    async def get_messages(
        cls,
        db: AsyncSession,
        session_id: str,
        uid: str,
        profile: Profile,
        current_message: str,
    ):
        limit_tokens = profile.context_window_k * 1024 * 0.8
        stmt = (
            select(Message)
            .where(Message.session_id == session_id, Message.uid == uid)
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(100)
        )
        result = await db.execute(stmt)
        history = result.scalars().all()

        temp_messages = []
        current_total = cls.estimate_tokens(current_message)

        for msg in history:
            content_str = msg.content or ""
            msg_tokens = cls.estimate_tokens(content_str)
            if current_total + msg_tokens > limit_tokens:
                break

            try:
                # 只有当数据库 role 为 tool 或是包含 tool_calls 的报文时，才需要恢复为报文结构
                if msg.role == "tool" or (
                    msg.role == "assistant" and "tool_calls" in content_str
                ):
                    parsed = json.loads(content_str)
                    if isinstance(parsed, dict) and (
                        parsed.get("role") == msg.role or "tool_calls" in parsed
                    ):
                        msg_item = parsed
                    else:
                        msg_item = {"role": msg.role, "content": content_str}
                else:
                    msg_item = {"role": msg.role, "content": content_str}
            except Exception:
                msg_item = {"role": msg.role, "content": content_str}

            temp_messages.insert(0, msg_item)
            current_total += msg_tokens

        if temp_messages and temp_messages[0].get("role") == "assistant":
            temp_messages.pop(0)

        final_messages = temp_messages
        final_messages.append({"role": "user", "content": current_message})
        return final_messages
