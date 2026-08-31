import json

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED,
    ERR_CHAT_INPUT_TOO_LONG,
)
from app.core.crud.session.message import message_crud
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import PROMPT_TOOL_INTERRUPTED
from app.core.utils.context_budget import (
    ContextRequestBudget,
    build_context_request_budget,
    ensure_context_request_budget_available,
    measure_context_request_usage,
)
from app.core.utils.context_messages import (
    find_protected_tail_start,
    is_context_summary_message,
    is_synthetic_summary_message,
    message_token_text,
    replace_protected_tool_chains_for_budget,
    to_jsonable,
    trim_protected_tail_tools,
)
from app.core.utils.dispatcher.truncate_tool_result import truncate_tool_result_with_stats
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import (
    InternalMessage,
    Message,
    MessageRole,
)
from app.models.profile import (
    Profile,
)

load_dotenv()
logger = get_logger(__name__)

CONTEXT_HISTORY_PAGE_SIZE = 200


def _is_background_tool_result_message(msg: InternalMessage) -> bool:
    if not isinstance(msg.content, str):
        return False
    try:
        payload = json.loads(msg.content)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("type") == "background_tool_result"


class ContextManager:
    _to_jsonable = staticmethod(to_jsonable)
    _message_token_text = staticmethod(message_token_text)
    _find_protected_tail_start = staticmethod(find_protected_tail_start)
    _trim_protected_tail_tools = staticmethod(trim_protected_tail_tools)

    @classmethod
    async def _load_history_backward_by_id(
        cls,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str,
        before_id: int | None,
        after_id: int | None,
        limit_tokens: int,
        current_msg_tokens: int,
        context_window_k: int,
        page_size: int = CONTEXT_HISTORY_PAGE_SIZE,
    ) -> list[Message]:
        raw_history: list[Message] = []
        page_before_id = before_id
        estimated_scan_tokens = current_msg_tokens
        max_scanned_messages = max(1, limit_tokens - current_msg_tokens)
        scanned_user_messages = 0

        while True:
            page = await message_crud.get_history_backward_by_id(
                db,
                session_id=session_id,
                uid=uid,
                after_id=after_id,
                before_id=before_id,
                page_before_id=page_before_id,
                limit=page_size,
            )
            if not page:
                break

            reached_safe_budget_boundary = False
            for raw_message in page:
                raw_history.append(raw_message)
                estimated_scan_tokens += max(1, estimate_tokens(raw_message.content or ""))
                if raw_message.role == MessageRole.USER:
                    scanned_user_messages += 1

                if raw_message.role != MessageRole.USER or estimated_scan_tokens < limit_tokens or scanned_user_messages < 2:
                    continue

                parsed_candidate = parse_db_messages_to_internal(raw_history)
                _, probe = cls._strategy_atomic_truncate(
                    uid=uid,
                    session_id=session_id,
                    parsed_history=parsed_candidate,
                    limit_tokens=limit_tokens,
                    current_msg_tokens=current_msg_tokens,
                    context_window_k=context_window_k,
                    emit_logs=False,
                )
                if probe["is_hard_truncated"] or len(raw_history) >= max_scanned_messages:
                    reached_safe_budget_boundary = True
                    break

            if reached_safe_budget_boundary or len(page) < page_size:
                break

            last_id = page[-1].id
            if last_id is None:
                break
            page_before_id = last_id

        return raw_history

    @classmethod
    async def get_messages(
        cls,
        db: AsyncSession,
        session_id: str,
        uid: str,
        profile: Profile,
        current_message: str,
        before_id: int | None = None,
        after_id: int | None = None,
        context_window_k: int = 4,
        reserved_tokens: int = 0,
    ) -> list[InternalMessage]:
        """
        获取经过压缩与对齐后的上下文消息列表。

        reserved_tokens：预留给系统提示词等运行时注入内容的 Token 数，
        从总预算中扣除，确保压缩后加上系统消息不会超出模型上下文限制。
        """
        limit_tokens = cls.get_history_budget_tokens(context_window_k=context_window_k, reserved_tokens=reserved_tokens)

        current_msg_tokens = estimate_tokens(current_message)

        # 1. 按消息编号从新到旧分页读取；达到预算后仅在完整轮次起点停止。
        raw_history = await cls._load_history_backward_by_id(
            db,
            session_id=session_id,
            uid=uid,
            before_id=before_id,
            after_id=after_id,
            limit_tokens=limit_tokens,
            current_msg_tokens=current_msg_tokens,
            context_window_k=context_window_k,
        )
        parsed_history_desc = parse_db_messages_to_internal(raw_history)
        parsed_history = list(reversed(parsed_history_desc))
        protected_start_idx = find_protected_tail_start(
            parsed_history,
            historical_round_count=1,
        )
        older_history = parsed_history[:protected_start_idx]
        protected_history = parsed_history[protected_start_idx:]
        protected_history_tokens = sum(estimate_tokens(cls._message_token_text(item)) for item in protected_history)

        # 2. 最近两个历史轮次先作为保护区保留；更早历史使用剩余预算裁剪。
        older_budget = max(1, limit_tokens - protected_history_tokens)
        trimmed_older, log_data = cls._strategy_atomic_truncate(
            uid=uid,
            session_id=session_id,
            parsed_history=list(reversed(older_history)),
            limit_tokens=older_budget,
            current_msg_tokens=current_msg_tokens,
            context_window_k=context_window_k,
        )
        final_msgs = cls.audit_tool_chain(
            [*trimmed_older, *protected_history],
            uid=uid,
            session_id=session_id,
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
        return max(1, context_window_k * CONTEXT_WINDOW_TOKENS_PER_K - max(reserved_tokens, 0))

    @classmethod
    def build_request_budget(
        cls,
        context_window_k: int,
        max_tokens: int,
        system_tokens: int = 0,
        tools: list[dict] | None = None,
        safety_margin_tokens: int = CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    ) -> ContextRequestBudget:
        return build_context_request_budget(
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            system_tokens=system_tokens,
            tools=tools,
            safety_margin_tokens=safety_margin_tokens,
        )

    @classmethod
    def ensure_request_budget_available(cls, budget: ContextRequestBudget) -> None:
        ensure_context_request_budget_available(budget)

    @classmethod
    def validate_latest_user_message_budget(
        cls,
        message: InternalMessage,
        context_window_k: int,
        max_tokens: int,
        system_tokens: int = 0,
        tools: list[dict] | None = None,
        safety_margin_tokens: int = CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
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
        safety_margin_tokens: int = CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    ) -> list[InternalMessage]:
        """
        在每次模型请求前对完整内存上下文做统一预算裁剪。

        预算包含模型上下文窗口、输出 token、工具 schema 与安全余量，避免工具响应追加到
        内存 messages 后绕过数据库历史压缩逻辑。
        """
        request_messages = [msg.model_copy(deep=True) for msg in messages]
        system_msgs = [msg for msg in request_messages if msg.role == MessageRole.SYSTEM]
        non_system_msgs = [msg for msg in request_messages if msg.role != MessageRole.SYSTEM]
        summary_msgs = [msg for msg in non_system_msgs if is_context_summary_message(msg)]
        summary_msg_ids = {id(msg) for msg in summary_msgs}
        dialogue_msgs = [msg for msg in non_system_msgs if id(msg) not in summary_msg_ids]

        usage = measure_context_request_usage(
            messages=request_messages,
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            tools=tools,
            safety_margin_tokens=safety_margin_tokens,
        )
        budget = usage.budget
        cls.ensure_request_budget_available(budget)

        summary_tokens = sum(estimate_tokens(cls._message_token_text(msg)) for msg in summary_msgs)
        dialogue_budget = budget.non_system_budget - summary_tokens
        if dialogue_budget <= 0:
            raise ParameterException(message=ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED)

        protected_start_idx = cls._find_protected_tail_start(dialogue_msgs)
        history_msgs = dialogue_msgs[:protected_start_idx]
        protected_tail = dialogue_msgs[protected_start_idx:]

        protected_tail = cls._trim_protected_tail_tools(
            protected_tail,
            uid=uid,
            session_id=session_id,
            context_window_k=context_window_k,
            non_system_budget=dialogue_budget,
        )
        protected_tail = replace_protected_tool_chains_for_budget(
            protected_tail,
            non_system_budget=dialogue_budget,
        )
        protected_tokens = sum(estimate_tokens(cls._message_token_text(msg)) for msg in protected_tail)
        if protected_tokens > dialogue_budget:
            latest_msg = protected_tail[-1] if protected_tail else None
            if latest_msg and latest_msg.role == MessageRole.USER and not latest_msg.tool_calls and not is_synthetic_summary_message(latest_msg):
                raise ParameterException(message=ERR_CHAT_INPUT_TOO_LONG)
            raise ParameterException(message=ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED)

        history_budget = dialogue_budget - protected_tokens

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

        audited_dialogue = cls.audit_tool_chain([*trimmed_history, *protected_tail], uid=uid, session_id=session_id)
        audited_non_system = [*summary_msgs, *audited_dialogue]
        final_usage = measure_context_request_usage(
            messages=[*system_msgs, *audited_non_system],
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            tools=tools,
            safety_margin_tokens=safety_margin_tokens,
        )
        if final_usage.exceeds_hard_window:
            latest_msg = audited_non_system[-1] if audited_non_system else None
            if latest_msg and latest_msg.role == MessageRole.USER and not latest_msg.tool_calls and not is_synthetic_summary_message(latest_msg):
                raise ParameterException(message=ERR_CHAT_INPUT_TOO_LONG)
            raise ParameterException(message=ERR_CHAT_CONTEXT_BUDGET_EXHAUSTED)

        return [*system_msgs, *audited_non_system]

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
        emit_logs: bool = True,
    ) -> tuple[list[InternalMessage], dict]:
        """
        默认策略：基于原子轮次对齐与工具审计的硬截断。
        """
        known_tool_call_ids: set[str] = set()
        for message in parsed_history:
            for tool_call in message.tool_calls or []:
                known_tool_call_ids.add(tool_call.id)
        temp_msgs = []
        current_total = current_msg_tokens
        raw_history_tokens = 0
        dropped_history_tokens = 0
        is_hard_truncated = False
        tool_truncation_stats: dict[int, int] = {}
        final_token_cache: dict[int, int] = {}

        # 反向装载（从新到旧）：窗口已满后继续累计本次有限扫描范围内
        # 被丢弃消息的原始 Token，供压缩日志展示本次裁剪前后的规模。
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

        audited_msgs = cls.audit_tool_chain(
            aligned_msgs,
            uid=uid,
            session_id=session_id,
            emit_logs=emit_logs,
            known_tool_call_ids=known_tool_call_ids,
        )

        final_truncated_tool_result_chars = 0
        final_truncated_tool_results = 0
        for fm in audited_msgs:
            removed_chars = tool_truncation_stats.get(id(fm))
            if removed_chars is not None:
                final_truncated_tool_results += 1
                final_truncated_tool_result_chars += removed_chars

        if emit_logs and final_truncated_tool_results:
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
    def audit_tool_chain(
        cls,
        messages: list[InternalMessage],
        uid: str,
        session_id: str,
        emit_logs: bool = True,
        known_tool_call_ids: set[str] | None = None,
    ) -> list[InternalMessage]:
        audited_msgs = []
        consumed_msg_ids = set()
        effective_known_tool_call_ids = set(known_tool_call_ids or ())
        for message in messages:
            for tool_call in message.tool_calls or []:
                effective_known_tool_call_ids.add(tool_call.id)

        i = 0
        while i < len(messages):
            msg = messages[i]

            if id(msg) in consumed_msg_ids:
                i += 1
                continue

            if msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                required_ids = list(dict.fromkeys(tc.id for tc in msg.tool_calls))
                matched_tools = []
                found_tool_call_ids = set()

                for j in range(i + 1, len(messages)):
                    target = messages[j]
                    if target.role == MessageRole.TOOL and target.tool_call_id in required_ids and target.tool_call_id not in found_tool_call_ids:
                        matched_tools.append(target)
                        found_tool_call_ids.add(target.tool_call_id)

                previous_non_system = next((item for item in reversed(audited_msgs) if item.role != MessageRole.SYSTEM), None)
                if previous_non_system is None or previous_non_system.role not in {MessageRole.USER, MessageRole.TOOL}:
                    for matched_tool in matched_tools:
                        consumed_msg_ids.add(id(matched_tool))
                    i += 1
                    continue

                audited_msgs.append(msg)
                for matched_tool in matched_tools:
                    audited_msgs.append(matched_tool)
                    consumed_msg_ids.add(id(matched_tool))

                if len(found_tool_call_ids) < len(required_ids):
                    if emit_logs:
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
                if emit_logs and msg.tool_call_id not in effective_known_tool_call_ids:
                    logger.bind(uid=uid, session_id=session_id).warning(t("LOG_CONTEXT_ORPHAN_TOOL_RESULT", tool_call_id=msg.tool_call_id))
                i += 1
            else:
                audited_msgs.append(msg)
                i += 1

        return audited_msgs
