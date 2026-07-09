import json
from dataclasses import dataclass

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED,
    ERR_CHAT_INPUT_TOO_LONG,
)
from app.core.crud.message import message_crud
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.utils.dispatcher.truncate_tool_result import truncate_tool_messages_for_budget, truncate_tool_result_with_stats
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


def _is_background_tool_result_message(msg: InternalMessage) -> bool:
    if not isinstance(msg.content, str):
        return False
    try:
        payload = json.loads(msg.content)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("type") == "background_tool_result"


@dataclass(frozen=True)
class ContextRequestBudget:
    context_window_tokens: int
    output_tokens: int
    tools_tokens: int
    safety_margin_tokens: int
    system_tokens: int
    total_input_budget: int
    non_system_budget: int


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
        limit_tokens = cls.get_history_budget_tokens(context_window_k=context_window_k, reserved_tokens=reserved_tokens)

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
            context_window_k=context_window_k,
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
    def get_history_budget_tokens(cls, context_window_k: int, reserved_tokens: int = 0) -> int:
        return max(1, context_window_k * 1024 - max(reserved_tokens, 0))

    @classmethod
    def build_request_budget(
        cls,
        context_window_k: int,
        max_tokens: int,
        system_tokens: int = 0,
        tools: list[dict] | None = None,
        safety_margin_tokens: int = 256,
    ) -> ContextRequestBudget:
        context_window_tokens = max(1, context_window_k * 1024)
        output_tokens = max(max_tokens, 0)
        tools_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
        safety_tokens = max(safety_margin_tokens, 0)
        normalized_system_tokens = max(system_tokens, 0)
        total_input_budget = context_window_tokens - output_tokens - tools_tokens - safety_tokens
        non_system_budget = total_input_budget - normalized_system_tokens
        return ContextRequestBudget(
            context_window_tokens=context_window_tokens,
            output_tokens=output_tokens,
            tools_tokens=tools_tokens,
            safety_margin_tokens=safety_tokens,
            system_tokens=normalized_system_tokens,
            total_input_budget=total_input_budget,
            non_system_budget=non_system_budget,
        )

    @classmethod
    def ensure_request_budget_available(cls, budget: ContextRequestBudget) -> None:
        if budget.total_input_budget <= 0 or budget.non_system_budget <= 0:
            raise ParameterException(message=ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED)

    @classmethod
    def validate_latest_user_message_budget(
        cls,
        message: InternalMessage,
        context_window_k: int,
        max_tokens: int,
        system_tokens: int = 0,
        tools: list[dict] | None = None,
        safety_margin_tokens: int = 256,
    ) -> None:
        budget = cls.build_request_budget(
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            system_tokens=system_tokens,
            tools=tools,
            safety_margin_tokens=safety_margin_tokens,
        )
        cls.ensure_request_budget_available(budget)
        if message.role == MessageRole.USER and not message.tool_calls and estimate_tokens(cls._message_token_text(message)) > budget.non_system_budget:
            raise ParameterException(message=ERR_CHAT_INPUT_TOO_LONG)

    @classmethod
    def trim_messages_for_model_request(
        cls,
        messages: list[InternalMessage],
        uid: str,
        session_id: str,
        context_window_k: int,
        max_tokens: int,
        tools: list[dict] | None = None,
        safety_margin_tokens: int = 256,
    ) -> list[InternalMessage]:
        """
        在每次模型请求前对完整内存上下文做统一预算裁剪。

        预算包含模型上下文窗口、输出 token、工具 schema 与安全余量，避免工具响应追加到
        内存 messages 后绕过数据库历史压缩逻辑。
        """
        request_messages = [msg.model_copy(deep=True) for msg in messages]
        system_msgs = [msg for msg in request_messages if msg.role == MessageRole.SYSTEM]
        non_system_msgs = [msg for msg in request_messages if msg.role != MessageRole.SYSTEM]

        system_tokens = sum(estimate_tokens(cls._message_token_text(msg)) for msg in system_msgs)
        budget = cls.build_request_budget(
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            system_tokens=system_tokens,
            tools=tools,
            safety_margin_tokens=safety_margin_tokens,
        )
        cls.ensure_request_budget_available(budget)

        protected_start_idx = cls._find_protected_tail_start(non_system_msgs)
        history_msgs = non_system_msgs[:protected_start_idx]
        protected_tail = non_system_msgs[protected_start_idx:]

        protected_tail = cls._trim_protected_tail_tools(
            protected_tail,
            uid=uid,
            session_id=session_id,
            context_window_k=context_window_k,
            non_system_budget=budget.non_system_budget,
        )
        protected_tokens = sum(estimate_tokens(cls._message_token_text(msg)) for msg in protected_tail)
        if protected_tokens > budget.non_system_budget:
            latest_msg = protected_tail[-1] if protected_tail else None
            if latest_msg and latest_msg.role == MessageRole.USER and not latest_msg.tool_calls:
                raise ParameterException(message=ERR_CHAT_INPUT_TOO_LONG)
            raise ParameterException(message=ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED)

        history_budget = budget.non_system_budget - protected_tokens

        if history_msgs and history_budget > 0:
            trimmed_history, _log_data = cls._strategy_atomic_truncate(
                uid=uid,
                session_id=session_id,
                parsed_history=list(reversed(history_msgs)),
                limit_tokens=history_budget,
                current_msg_tokens=0,
                context_window_k=context_window_k,
                tool_result_limit_tokens=max(1, history_budget // 2),
            )
        else:
            trimmed_history = []

        audited_non_system = cls.audit_tool_chain([*trimmed_history, *protected_tail], uid=uid, session_id=session_id)
        audited_tokens = sum(estimate_tokens(cls._message_token_text(msg)) for msg in audited_non_system)
        if audited_tokens > budget.non_system_budget:
            latest_msg = audited_non_system[-1] if audited_non_system else None
            if latest_msg and latest_msg.role == MessageRole.USER and not latest_msg.tool_calls:
                raise ParameterException(message=ERR_CHAT_INPUT_TOO_LONG)
            raise ParameterException(message=ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED)

        return [*system_msgs, *audited_non_system]

    @staticmethod
    def _to_jsonable(value):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, list):
            return [ContextManager._to_jsonable(item) for item in value]
        if isinstance(value, dict):
            return {key: ContextManager._to_jsonable(item) for key, item in value.items()}
        return value

    @staticmethod
    def _message_token_text(msg: InternalMessage) -> str:
        if msg.tool_calls:
            return msg.model_dump_json(exclude_none=True)
        if isinstance(msg.content, str):
            return msg.content
        if msg.content is None:
            return ""
        if isinstance(msg.content, list):
            text_parts: list[str] = []
            for part in msg.content:
                part_type = getattr(part, "type", "")
                if part_type == "text":
                    text_parts.append(str(getattr(part, "text", "") or ""))
                elif part_type == "image_url":
                    text_parts.append("[图片]")
                elif part_type == "file":
                    text_parts.append(f"[文件:{getattr(part, 'path', '') or ''}]")
                else:
                    text_parts.append(json.dumps(ContextManager._to_jsonable(part), ensure_ascii=False))
            return "\n".join(item for item in text_parts if item)
        return json.dumps(ContextManager._to_jsonable(msg.content), ensure_ascii=False)

    @staticmethod
    def _find_protected_tail_start(non_system_msgs: list[InternalMessage]) -> int:
        if not non_system_msgs:
            return 0

        last_idx = len(non_system_msgs) - 1
        if non_system_msgs[last_idx].role == MessageRole.TOOL:
            tool_call_ids = set()
            scan_idx = last_idx
            while scan_idx >= 0 and non_system_msgs[scan_idx].role == MessageRole.TOOL:
                if non_system_msgs[scan_idx].tool_call_id:
                    tool_call_ids.add(non_system_msgs[scan_idx].tool_call_id)
                scan_idx -= 1

            if scan_idx >= 0:
                candidate = non_system_msgs[scan_idx]
                if candidate.role == MessageRole.ASSISTANT and candidate.tool_calls:
                    required_ids = {tool_call.id for tool_call in candidate.tool_calls}
                    if tool_call_ids and tool_call_ids.issubset(required_ids):
                        user_idx = scan_idx - 1
                        while user_idx >= 0:
                            if non_system_msgs[user_idx].role == MessageRole.USER:
                                return user_idx
                            user_idx -= 1
                        return scan_idx

        return last_idx

    @classmethod
    def _trim_protected_tail_tools(
        cls,
        protected_tail: list[InternalMessage],
        uid: str,
        session_id: str,
        context_window_k: int,
        non_system_budget: int,
    ) -> list[InternalMessage]:
        tool_msgs = [msg for msg in protected_tail if msg.role == MessageRole.TOOL]
        if not tool_msgs:
            return protected_tail

        non_tool_tokens = sum(estimate_tokens(cls._message_token_text(msg)) for msg in protected_tail if msg.role != MessageRole.TOOL)
        tool_budget = max(1, non_system_budget - non_tool_tokens)
        truncate_tool_messages_for_budget(
            tool_msgs=tool_msgs,
            context_window_k=context_window_k,
            budget_tokens=tool_budget,
            uid=uid,
            session_id=session_id,
        )

        return protected_tail

    @classmethod
    def _strategy_atomic_truncate(
        cls,
        uid: str,
        session_id: str,
        parsed_history: list[InternalMessage],
        limit_tokens: float,
        current_msg_tokens: int,
        context_window_k: int,
        tool_result_limit_tokens: int | None = None,
    ) -> tuple[list[InternalMessage], dict]:
        """
        默认策略：基于原子轮次对齐与工具审计的硬截断。
        """
        temp_msgs = []
        current_total = current_msg_tokens
        raw_history_tokens = 0
        dropped_history_tokens = 0
        is_hard_truncated = False
        tool_truncation_stats: dict[int, int] = {}
        final_token_cache: dict[int, int] = {}

        # 反向装载（从新到旧）：窗口已满后继续累计被丢弃消息的原始 Token，
        # 以便压缩日志的 before 反映数据库中的完整历史规模，而非仅窗口内保留部分。
        for msg in parsed_history:
            if is_hard_truncated:
                msg_str = cls._message_token_text(msg)
                dropped_history_tokens += estimate_tokens(msg_str)
                continue

            if msg.role == MessageRole.TOOL:
                truncation = truncate_tool_result_with_stats(msg.content or "", context_window_k, limit_tokens=tool_result_limit_tokens)
                msg.content = truncation.content
                original_msg_tokens = truncation.original_tokens
                msg_tokens = truncation.final_tokens
                if truncation.truncated:
                    tool_truncation_stats[id(msg)] = truncation.removed_chars
            else:
                msg_str = cls._message_token_text(msg)
                original_msg_tokens = estimate_tokens(msg_str)
                msg_tokens = original_msg_tokens

            if current_total + msg_tokens > limit_tokens:
                is_hard_truncated = True
                dropped_history_tokens += original_msg_tokens
                continue

            temp_msgs.insert(0, msg)
            final_token_cache[id(msg)] = msg_tokens
            current_total += msg_tokens
            raw_history_tokens += original_msg_tokens

        # A. 原子化轮次对齐 (保留所有窗口内的 SYSTEM 消息，并从第一个 USER 开始对齐后续消息)
        system_msgs = [m for m in temp_msgs if m.role == MessageRole.SYSTEM]

        # 合并：保持 SYSTEM 在前，随后保留窗口内已装载的完整消息序列。
        # 孤儿 TOOL 结果由 audit_tool_chain 过滤，避免因第一个 USER 刚好被窗口截断而丢弃其后的完整 assistant/tool 链路。
        aligned_msgs = []
        added_ids = set()

        for m in system_msgs:
            aligned_msgs.append(m)
            added_ids.add(id(m))

        for m in temp_msgs:
            if id(m) not in added_ids:
                aligned_msgs.append(m)

        audited_msgs = cls.audit_tool_chain(aligned_msgs, uid=uid, session_id=session_id)

        final_truncated_tool_result_chars = 0
        final_truncated_tool_results = 0
        for fm in audited_msgs:
            removed_chars = tool_truncation_stats.get(id(fm))
            if removed_chars is not None:
                final_truncated_tool_results += 1
                final_truncated_tool_result_chars += removed_chars

        if final_truncated_tool_results:
            logger.bind(uid=uid, session_id=session_id).info(
                t(
                    "LOG_CONTEXT_TOOL_RESULTS_TRUNCATED_SCANNED",
                    count=final_truncated_tool_results,
                    removed_chars=final_truncated_tool_result_chars,
                    context_window_k=context_window_k,
                )
            )

        # 计算压缩后的 Token 数。扫描阶段已计算过的消息直接复用缓存，虚拟补偿消息再估算。
        final_history_tokens = 0
        for fm in audited_msgs:
            cached_tokens = final_token_cache.get(id(fm))
            if cached_tokens is not None:
                final_history_tokens += cached_tokens
                continue

            fm_str = cls._message_token_text(fm)
            final_history_tokens += estimate_tokens(fm_str)

        return audited_msgs, {
            "is_hard_truncated": is_hard_truncated,
            "before": current_msg_tokens + raw_history_tokens + dropped_history_tokens,
            "after": current_msg_tokens + final_history_tokens,
        }

    @classmethod
    def audit_tool_chain(cls, messages: list[InternalMessage], uid: str, session_id: str) -> list[InternalMessage]:
        audited_msgs = []
        consumed_msg_ids = set()

        i = 0
        while i < len(messages):
            msg = messages[i]

            if id(msg) in consumed_msg_ids:
                i += 1
                continue

            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                required_ids = [tc.id for tc in msg.tool_calls]
                matched_tools = []
                found_tool_call_ids = set()

                for j in range(i + 1, len(messages)):
                    target = messages[j]
                    if target.role == MessageRole.TOOL and target.tool_call_id in required_ids and target.tool_call_id not in found_tool_call_ids:
                        matched_tools.append(target)
                        found_tool_call_ids.add(target.tool_call_id)

                audited_msgs.append(msg)
                for matched_tool in matched_tools:
                    audited_msgs.append(matched_tool)
                    consumed_msg_ids.add(id(matched_tool))

                if len(found_tool_call_ids) < len(required_ids):
                    logger.bind(uid=uid, session_id=session_id).warning(
                        t(
                            "LOG_CONTEXT_TOOL_CHAIN_INCOMPLETE",
                            required_ids=required_ids,
                            found_ids=list(found_tool_call_ids),
                        )
                    )
                    for tool_call_id in required_ids:
                        if tool_call_id not in found_tool_call_ids:
                            virtual_tool_msg = InternalMessage(
                                role=MessageRole.TOOL,
                                tool_call_id=tool_call_id,
                                content=json.dumps({"error": PROMPT_TOOL_INTERRUPTED}),
                            )
                            audited_msgs.append(virtual_tool_msg)
                i += 1
            elif msg.role == MessageRole.TOOL:
                if _is_background_tool_result_message(msg):
                    i += 1
                    continue
                logger.bind(uid=uid, session_id=session_id).warning(t("LOG_CONTEXT_ORPHAN_TOOL_RESULT", tool_call_id=msg.tool_call_id))
                i += 1
            else:
                audited_msgs.append(msg)
                i += 1

        return audited_msgs
