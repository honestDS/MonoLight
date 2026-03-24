from typing import List, Tuple
import json
import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.profile import Profile, ProfileConfig
from app.models.message import MessageRole, InternalMessage, InternalToolCall
from app.core.log import get_logger
from app.core.utils.tokenizer import estimate_tokens
from app.core.utils.message_parser import parse_db_messages_to_internal

# CRUD Imports
from app.core.crud.message import message_crud

load_dotenv()
logger = get_logger(__name__)


class ContextManager:
    @classmethod
    async def get_messages(
        cls,
        db: AsyncSession,
        session_id: str,
        uid: str,
        profile: Profile,
        current_message: str,
    ) -> List[InternalMessage]:
        """
        获取经过压缩与对齐后的上下文消息列表。
        """
        cfg = ProfileConfig.model_validate(profile.configs)
        limit_tokens = cfg.other.context_window_k * 1024 * 0.8

        # 1. 加载并初步解析原始历史记录 (通过工具类进行协议转换)
        raw_history = await message_crud.get_history(
            db, session_id=session_id, uid=uid, limit=100
        )
        parsed_history = parse_db_messages_to_internal(raw_history)

        # 2. 策略分发
        current_msg_tokens = estimate_tokens(current_message)

        final_msgs, log_data = cls._strategy_atomic_truncate(
            session_id=session_id,
            parsed_history=parsed_history,
            limit_tokens=limit_tokens,
            current_msg_tokens=current_msg_tokens,
        )

        # 3. 压缩日志记录
        if log_data["is_compressed"]:
            logger.info(
                f"Context compressed for session {session_id}. "
                f"Tokens: {log_data['before']} -> {log_data['after']}"
            )

        final_msgs.append(
            InternalMessage(role=MessageRole.USER, content=current_message)
        )
        return final_msgs

    @classmethod
    def _strategy_atomic_truncate(
        cls,
        session_id: str,
        parsed_history: List[InternalMessage],
        limit_tokens: float,
        current_msg_tokens: int,
    ) -> Tuple[List[InternalMessage], dict]:
        """
        默认策略：基于原子轮次对齐与工具审计的硬截断。
        """
        temp_msgs = []
        current_total = current_msg_tokens
        raw_history_tokens = 0
        is_hard_truncated = False

        # 反向装载（从新到旧）
        for msg in parsed_history:
            msg_str = (
                json.dumps(msg.model_dump()) if msg.tool_calls else (msg.content or "")
            )
            msg_tokens = estimate_tokens(msg_str)

            if current_total + msg_tokens > limit_tokens:
                is_hard_truncated = True
                break

            temp_msgs.insert(0, msg)
            current_total += msg_tokens
            raw_history_tokens += msg_tokens

        # A. 原子化轮次对齐 (保留所有窗口内的 SYSTEM 消息，并从第一个 USER 开始对齐后续消息)
        system_msgs = [m for m in temp_msgs if m.role == MessageRole.SYSTEM]
        
        first_user_idx = -1
        for idx, m in enumerate(temp_msgs):
            if m.role == MessageRole.USER:
                first_user_idx = idx
                break
        
        # 提取从第一个 USER 开始的所有消息
        user_onwards = temp_msgs[first_user_idx:] if first_user_idx != -1 else []
        
        # 合并：保持 SYSTEM 在前，随后跟随对齐后的对话流（去重处理）
        aligned_msgs = []
        added_ids = set()
        
        for m in system_msgs:
            aligned_msgs.append(m)
            added_ids.add(id(m))
            
        for m in user_onwards:
            if id(m) not in added_ids:
                aligned_msgs.append(m)

        # B. 工具链一致性审计 (ID 匹配审计)
        audited_msgs = []
        i = 0
        while i < len(aligned_msgs):
            msg = aligned_msgs[i]
            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                required_ids = [tc.id for tc in msg.tool_calls]
                j = i + 1
                matched_tools = []
                found_ids = []
                while (
                    j < len(aligned_msgs) and aligned_msgs[j].role == MessageRole.TOOL
                ):
                    t_id = aligned_msgs[j].tool_call_id
                    if t_id in required_ids:
                        matched_tools.append(aligned_msgs[j])
                        found_ids.append(t_id)
                    j += 1

                if all(rid in found_ids for rid in required_ids):
                    audited_msgs.append(msg)
                    audited_msgs.extend(matched_tools)
                    i = j
                else:
                    logger.warning(
                        f"Broken tool chain in {session_id}. Required: {required_ids}"
                    )
                    i = j
            elif msg.role == MessageRole.TOOL:
                logger.warning(
                    f"Orphan tool result in {session_id}. ID: {msg.tool_call_id}"
                )
                i += 1
            else:
                audited_msgs.append(msg)
                i += 1

        # 计算压缩后的 Token 数
        final_history_tokens = 0
        for fm in audited_msgs:
            fm_str = (
                json.dumps(fm.model_dump()) if fm.tool_calls else (fm.content or "")
            )
            final_history_tokens += estimate_tokens(fm_str)

        is_compressed = is_hard_truncated or (len(audited_msgs) < len(parsed_history))

        return audited_msgs, {
            "is_compressed": is_compressed,
            "before": current_msg_tokens + raw_history_tokens,
            "after": current_msg_tokens + final_history_tokens,
        }
