import json

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession

# CRUD Imports
from app.core.crud.message import message_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import (
    InternalMessage,
    MessageRole,
)
from app.models.profile import (
    Profile,
)

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
        before_id: int | None = None,
        context_window_k: int = 4,
        reserved_tokens: int = 0,
    ) -> list[InternalMessage]:
        """
        获取经过压缩与对齐后的上下文消息列表。

        reserved_tokens：预留给系统提示词等运行时注入内容的 Token 数，
        从总预算中扣除，确保压缩后加上系统消息不会超出模型上下文限制。
        """
        limit_tokens = max(1, context_window_k * 1024 - reserved_tokens)

        # 1. 加载并初步解析原始历史记录 (通过工具类进行协议转换)
        raw_history = await message_crud.get_history(db, session_id=session_id, uid=uid, limit=5000, before_id=before_id)
        parsed_history = parse_db_messages_to_internal(raw_history)

        # 2. 策略分发
        current_msg_tokens = estimate_tokens(current_message)

        final_msgs, log_data = cls._strategy_atomic_truncate(
            uid=uid,
            session_id=session_id,
            parsed_history=parsed_history,
            limit_tokens=limit_tokens,
            current_msg_tokens=current_msg_tokens,
        )

        # 3. 压缩日志记录：仅当确实发生压缩（Token 真正减少）时才记录，避免误导性日志
        if log_data["is_hard_truncated"] and log_data["after"] < log_data["before"]:
            logger.bind(uid=uid, session_id=session_id).info(
                t(
                    "LOG_CONTEXT_COMPRESSED",
                    before=log_data["before"],
                    after=log_data["after"],
                    reserved_tokens=reserved_tokens,
                )
            )

        return final_msgs

    @classmethod
    def _strategy_atomic_truncate(
        cls,
        uid: str,
        session_id: str,
        parsed_history: list[InternalMessage],
        limit_tokens: float,
        current_msg_tokens: int,
    ) -> tuple[list[InternalMessage], dict]:
        """
        默认策略：基于原子轮次对齐与工具审计的硬截断。
        """
        temp_msgs = []
        current_total = current_msg_tokens
        raw_history_tokens = 0
        dropped_history_tokens = 0
        is_hard_truncated = False

        # 反向装载（从新到旧）：窗口已满后继续累计被丢弃消息的 Token，
        # 以便压缩日志的 before 反映完整历史规模，而非仅窗口内保留部分。
        for msg in parsed_history:
            msg_str = json.dumps(msg.model_dump()) if msg.tool_calls else (msg.content or "")
            msg_tokens = estimate_tokens(msg_str)

            if is_hard_truncated or current_total + msg_tokens > limit_tokens:
                is_hard_truncated = True
                dropped_history_tokens += msg_tokens
                continue

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
        # 使用 set 记录已经作为工具链一部分被处理掉的消息对象 ID，防止重复添加
        consumed_msg_ids = set()

        i = 0
        while i < len(aligned_msgs):
            msg = aligned_msgs[i]

            # 如果该消息已经被之前的工具链审计包含了，直接跳过
            if id(msg) in consumed_msg_ids:
                i += 1
                continue

            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                required_ids = [tc.id for tc in msg.tool_calls]
                matched_tools = []
                found_tool_call_ids = set()

                # 寻找后续所有的工具返回结果
                for j in range(i + 1, len(aligned_msgs)):
                    target = aligned_msgs[j]
                    if target.role == MessageRole.TOOL and target.tool_call_id in required_ids:
                        if target.tool_call_id not in found_tool_call_ids:
                            matched_tools.append(target)
                            found_tool_call_ids.add(target.tool_call_id)

                # 保持协议合规：如果工具链不完整，通过虚拟响应进行补偿，而不是直接舍弃
                audited_msgs.append(msg)
                # 先添加已有的工具响应
                for mt in matched_tools:
                    audited_msgs.append(mt)
                    consumed_msg_ids.add(id(mt))

                # 如果有缺失，注入虚拟补偿响应
                if len(found_tool_call_ids) < len(required_ids):
                    logger.bind(uid=uid, session_id=session_id).warning(
                        t(
                            "LOG_CONTEXT_TOOL_CHAIN_INCOMPLETE",
                            required_ids=required_ids,
                            found_ids=list(found_tool_call_ids),
                        )
                    )
                    for tc_id in required_ids:
                        if tc_id not in found_tool_call_ids:
                            virtual_tool_msg = InternalMessage(
                                role=MessageRole.TOOL,
                                tool_call_id=tc_id,
                                content=json.dumps({"error": PROMPT_TOOL_INTERRUPTED}),
                            )
                            audited_msgs.append(virtual_tool_msg)
                i += 1
            elif msg.role == MessageRole.TOOL:
                # 孤立的工具结果（没有对应的 Assistant 调用），直接舍弃以保持协议合规
                logger.bind(uid=uid, session_id=session_id).warning(t("LOG_CONTEXT_ORPHAN_TOOL_RESULT", tool_call_id=msg.tool_call_id))
                i += 1
            else:
                # 普通消息直接添加
                audited_msgs.append(msg)
                i += 1

        # 计算压缩后的 Token 数
        final_history_tokens = 0
        for fm in audited_msgs:
            fm_str = json.dumps(fm.model_dump()) if fm.tool_calls else (fm.content or "")
            final_history_tokens += estimate_tokens(fm_str)

        return audited_msgs, {
            "is_hard_truncated": is_hard_truncated,
            "before": current_msg_tokens + raw_history_tokens + dropped_history_tokens,
            "after": current_msg_tokens + final_history_tokens,
        }
