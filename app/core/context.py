from typing import List
import json
import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.profile import Profile, ProfileConfig
from app.models.message import MessageRole, InternalMessage, InternalToolCall

# CRUD Imports
from app.core.crud.message import message_crud

load_dotenv()

class ContextManager:
    @staticmethod
    def estimate_tokens(text: str) -> int:
        if not text:
            return 0
        chinese_count = len([c for c in text if "\u4e00" <= c <= "\u9fff"])
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

        # 使用 CRUD 获取最近 100 条消息历史
        history = await message_crud.get_history(db, session_id=session_id, uid=uid, limit=100)

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

                # 解析存储在 content 中的工具调用 JSON 数据
                if role == MessageRole.TOOL or (
                    role == MessageRole.ASSISTANT and "tool_calls" in content_str
                ):
                    try:
                        parsed = json.loads(content_str)
                        if isinstance(parsed, dict):
                            if "tool_calls" in parsed:
                                tool_calls = [InternalToolCall(**tc) for tc in parsed["tool_calls"]]
                                content = parsed.get("content")
                            if "tool_call_id" in parsed:
                                tool_call_id = parsed["tool_call_id"]
                                content = parsed.get("content")
                    except json.JSONDecodeError:
                        # 如果不是 JSON，则作为普通文本处理
                        pass

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
        final_msgs = []
        i = 0
        while i < len(temp_msgs):
            msg = temp_msgs[i]
            
            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                tool_call_ids = {tc.id for tc in msg.tool_calls}
                j = i + 1
                matched_tools = []
                while j < len(temp_msgs) and temp_msgs[j].role == MessageRole.TOOL:
                    if temp_msgs[j].tool_call_id in tool_call_ids:
                        matched_tools.append(temp_msgs[j])
                    j += 1
                
                if len(matched_tools) == len(tool_call_ids):
                    final_msgs.append(msg)
                    final_msgs.extend(matched_tools)
                    i = j
                else:
                    i += 1 
            elif msg.role == MessageRole.TOOL:
                i += 1
            else:
                final_msgs.append(msg)
                i += 1

        # 移除开头孤立的工具请求
        if final_msgs and final_msgs[0].role == MessageRole.ASSISTANT and final_msgs[0].tool_calls:
            final_msgs.pop(0)

        final_msgs.append(InternalMessage(role=MessageRole.USER, content=current_message))
        return final_msgs
