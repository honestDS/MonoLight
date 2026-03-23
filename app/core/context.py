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

        temp_msgs = []
        current_total = cls.estimate_tokens(current_message)

        for msg in history:
            content_str = msg.content or ""
            msg_tokens = cls.estimate_tokens(content_str)
            if current_total + msg_tokens > limit_tokens:
                break

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

                temp_msgs.insert(
                    0,
                    InternalMessage(
                        role=role,
                        content=content,
                        tool_calls=tool_calls,
                        tool_call_id=tool_call_id,
                    ),
                )
                current_total += msg_tokens
            except Exception:
                continue

        # 核心逻辑：修复 Tool Call 链条完整性
        # 如果上下文截断导致出现了孤立的 Tool 消息（没有对应的 Assistant 请求），
        # 或者孤立的带有 Tool Calls 的 Assistant 消息（没有对应的 Tool 结果），
        # 需要进行清理，以满足 LLM 协议格式要求。
        
        final_msgs = []
        i = 0
        while i < len(temp_msgs):
            msg = temp_msgs[i]
            
            # 如果是带有工具调用的助手消息
            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                # 寻找后续所有的工具结果
                tool_call_ids = {tc.id for tc in msg.tool_calls}
                j = i + 1
                matched_tools = []
                while j < len(temp_msgs) and temp_msgs[j].role == MessageRole.TOOL:
                    if temp_msgs[j].tool_call_id in tool_call_ids:
                        matched_tools.append(temp_msgs[j])
                    j += 1
                
                # 只有当所有的工具调用都有对应的结果时，才保留这一组消息
                if len(matched_tools) == len(tool_call_ids):
                    final_msgs.append(msg)
                    final_msgs.extend(matched_tools)
                    i = j # 跳过已处理的工具消息
                else:
                    # 链条不完整，丢弃该助手消息及后续不匹配的工具消息
                    i += 1 
            elif msg.role == MessageRole.TOOL:
                # 孤立的工具消息，直接丢弃
                i += 1
            else:
                # 普通 User 或 Assistant 文本消息
                final_msgs.append(msg)
                i += 1

        if final_msgs and final_msgs[0].role == MessageRole.ASSISTANT and final_msgs[0].tool_calls:
            final_msgs.pop(0)

        final_msgs.append(
            InternalMessage(role=MessageRole.USER, content=current_message)
        )
        return final_msgs
