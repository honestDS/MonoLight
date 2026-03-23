from typing import List
import json
import os
from dotenv import load_dotenv
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.message import Message
from app.models.profile import Profile
from app.schemas.profile import ProfileConfig
from app.schemas.message import InternalMessage, MessageRole, InternalToolCall

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
    ) -> List[InternalMessage]:
        cfg = ProfileConfig.model_validate(profile.configs)
        limit_tokens = cfg.other.context_window_k * 1024 * 0.8

        stmt = (
            select(Message)
            .where(Message.session_id == session_id, Message.uid == uid)
            .order_by(desc(Message.created_at), desc(Message.id))
            .limit(100)
        )
        result = await db.execute(stmt)
        history = result.scalars().all()

        internal_msgs = []
        current_total = cls.estimate_tokens(current_message)

        for msg in history:
            content_str = msg.content or ""
            msg_tokens = cls.estimate_tokens(content_str)
            if current_total + msg_tokens > limit_tokens:
                break

            # 解析历史消息为 InternalMessage
            try:
                role = MessageRole(msg.role)
                tool_calls = None
                tool_call_id = None
                content = content_str

                if role == MessageRole.TOOL or (
                    role == MessageRole.ASSISTANT and "tool_calls" in content_str
                ):
                    parsed = json.loads(content_str)
                    if isinstance(parsed, dict):
                        if "tool_calls" in parsed:
                            tool_calls = (
                                [InternalToolCall(**tc) for tc in parsed["tool_calls"]]
                                if isinstance(parsed["tool_calls"], list)
                                else None
                            )
                            content = parsed.get("content")
                        if "tool_call_id" in parsed:
                            tool_call_id = parsed["tool_call_id"]
                            content = parsed.get("content")

                internal_msgs.insert(
                    0,
                    InternalMessage(
                        role=role,
                        content=content,
                        tool_calls=tool_calls,
                        tool_call_id=tool_call_id,
                    ),
                )
            except Exception:
                internal_msgs.insert(
                    0, InternalMessage(role=MessageRole(msg.role), content=content_str)
                )

            current_total += msg_tokens

        if internal_msgs and internal_msgs[0].role == MessageRole.ASSISTANT:
            internal_msgs.pop(0)

        internal_msgs.append(
            InternalMessage(role=MessageRole.USER, content=current_message)
        )
        return internal_msgs
