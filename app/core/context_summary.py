import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_router import select_channel
from app.core.crud.message import message_crud
from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import channel_log_extra, get_logger
from app.core.prompts import (
    CONTEXT_SUMMARY_COMPRESS_PROMPT,
    CONTEXT_SUMMARY_PROMPT,
    CONTEXT_SUMMARY_WRAPPER,
)
from app.core.utils.dispatcher.helpers import format_exception_message, resolve_chat_params
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole
from app.models.profile import Profile, ProfileConfig
from app.providers.database import AsyncSessionLocal
from app.providers.llm.client import LLMClient

logger = get_logger(__name__)

# 总结压缩专用超时，不读取前端/配置里的 chat_timeout。
CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS = 600.0


@dataclass(frozen=True)
class ContextSummaryState:
    content: str | None
    message_id: int | None

    def as_message(self) -> InternalMessage | None:
        if not self.content:
            return None
        return InternalMessage(
            role=MessageRole.SYSTEM,
            content=CONTEXT_SUMMARY_WRAPPER.format(content=self.content),
        )


def _serialize_message(message: InternalMessage) -> str:
    payload = message.model_dump(
        mode="json",
        exclude={"id", "attachments", "created_at"},
        exclude_none=True,
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _join_messages(messages: list[InternalMessage]) -> str:
    if not messages:
        return "(none)"
    return "\n".join(_serialize_message(message) for message in messages)


def _estimate_summary_tokens(content: str | None) -> int:
    if not content:
        return 0
    return estimate_tokens(CONTEXT_SUMMARY_WRAPPER.format(content=content))


def _select_summary_segment(messages: list[InternalMessage], target_tokens: int) -> list[InternalMessage]:
    if len(messages) < 2:
        return []

    running_tokens = 0
    preferred_end = 0
    safe_ends: list[int] = []
    for index, message in enumerate(messages):
        running_tokens += estimate_tokens(_serialize_message(message))
        next_message = messages[index + 1] if index + 1 < len(messages) else None
        if message.id is not None and next_message is not None and next_message.role == MessageRole.USER:
            safe_ends.append(index + 1)
        if running_tokens <= target_tokens:
            preferred_end = index + 1

    eligible_ends = [end for end in safe_ends if end <= preferred_end]
    if eligible_ends:
        return messages[: eligible_ends[-1]]
    return []


def _select_recent_rounds(messages: list[InternalMessage], round_count: int = 2) -> list[InternalMessage]:
    if not messages or round_count <= 0:
        return []
    user_indices = [index for index, message in enumerate(messages) if message.role == MessageRole.USER]
    if not user_indices:
        return []
    start_index = user_indices[-min(round_count, len(user_indices))]
    return messages[start_index:]


def _calc_token_usage(
    *,
    messages: list[InternalMessage],
    summary_content: str | None,
    current_message: str,
    reserved_tokens: int,
    tools: list[dict] | None,
    context_window_k: int,
    max_tokens: int,
    safety_margin_tokens: int,
    threshold_percent: int,
) -> dict[str, int]:
    summary_tokens = _estimate_summary_tokens(summary_content)
    history_tokens = sum(estimate_tokens(_serialize_message(message)) for message in messages)
    tools_tokens = estimate_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    context_window_tokens = context_window_k * 1024
    output_tokens = max(max_tokens, 0)
    safety_tokens = max(safety_margin_tokens, 0)
    input_budget = max(1, context_window_tokens - output_tokens - safety_tokens)
    current_message_tokens = estimate_tokens(current_message)
    required_tokens = reserved_tokens + summary_tokens + history_tokens + current_message_tokens + tools_tokens
    summary_trigger_tokens = max(1, input_budget * threshold_percent // 100)
    compression_goal_tokens = max(1, summary_trigger_tokens // 2)
    return {
        "summary_tokens": summary_tokens,
        "history_tokens": history_tokens,
        "tools_tokens": tools_tokens,
        "context_window_tokens": context_window_tokens,
        "output_tokens": output_tokens,
        "safety_tokens": safety_tokens,
        "input_budget": input_budget,
        "current_message_tokens": current_message_tokens,
        "required_tokens": required_tokens,
        "summary_trigger_tokens": summary_trigger_tokens,
        "compression_goal_tokens": compression_goal_tokens,
        "reserved_tokens": reserved_tokens,
        "threshold_percent": threshold_percent,
        "history_message_count": len(messages),
    }


def _remaining_after_segment(
    messages: list[InternalMessage],
    segment: list[InternalMessage],
) -> list[InternalMessage]:
    boundary_id = segment[-1].id
    if boundary_id is None:
        return messages[len(segment) :]
    return [message for message in messages if message.id is None or message.id > boundary_id]


async def _release_db_session(db: AsyncSession) -> None:
    """结束当前事务，避免长耗时外部调用期间占用 SQLite 写锁。"""
    in_transaction = getattr(db, "in_transaction", None)
    if not callable(in_transaction):
        return
    try:
        active = in_transaction()
    except Exception:
        return
    if not active:
        return
    commit = getattr(db, "commit", None)
    if callable(commit):
        await commit()


async def _persist_context_summary(
    *,
    session_id: str,
    uid: str,
    expected_message_id: int | None,
    summary: str,
    message_id: int,
) -> bool:
    """使用独立短会话写入总结，写完立即提交。"""
    async with AsyncSessionLocal() as summary_db:
        updated = await session_crud.update_context_summary(
            summary_db,
            session_id=session_id,
            uid=uid,
            expected_message_id=expected_message_id,
            summary=summary,
            message_id=message_id,
        )
        if updated:
            await summary_db.commit()
        else:
            await summary_db.rollback()
        return updated


async def get_context_summary_state(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
) -> ContextSummaryState:
    session = await session_crud.get_by_session_id(db, session_id)
    if session is None or session.uid != uid:
        return ContextSummaryState(content=None, message_id=None)
    return ContextSummaryState(
        content=session.context_summary,
        message_id=session.context_summary_message_id,
    )


async def _generate_summary_text(
    db: AsyncSession,
    *,
    profile: Profile,
    cfg: ProfileConfig,
    prompt: str,
    safety_margin_tokens: int,
    uid: str,
    session_id: str,
) -> str | None:
    chat_channel = cfg.channel.chat_channel
    cursor_key = f"{profile.id}:CHAT:CONTEXT_SUMMARY"
    excluded_priorities: set[int] = set()
    selection = await select_channel(
        db,
        chat_channel,
        "CHAT",
        call_context="context_summary",
        cursor_key=cursor_key,
    )

    while selection:
        channel, model_entry, rule = selection
        chat_params = resolve_chat_params(model_entry, chat_channel)
        summary_max_tokens = min(1024, max(256, chat_params["context_window_k"] * 64))
        summary_input_tokens = estimate_tokens(prompt)
        summary_input_budget = max(
            1,
            chat_params["context_window_k"] * 1024 - summary_max_tokens - safety_margin_tokens,
        )
        if summary_input_tokens > summary_input_budget:
            excluded_priorities.add(rule.priority)
            selection = await select_channel(
                db,
                chat_channel,
                "CHAT",
                call_context="context_summary_retry",
                excluded_priorities=excluded_priorities,
                cursor_key=cursor_key,
            )
            continue

        api_key = channel.get_decrypted_api_key()
        base_url = channel.base_url
        protocol = getattr(channel, "protocol", "openai")
        model_id = model_entry["model_id"]

        # 调模型前释放调用方事务，避免长请求期间占着写锁。
        await _release_db_session(db)

        try:
            response = await LLMClient.generate(
                api_key=api_key,
                base_url=base_url,
                model_id=model_id,
                messages=[InternalMessage(role=MessageRole.USER, content=prompt)],
                temperature=0.2,
                max_tokens=summary_max_tokens,
                protocol=protocol,
                timeout=CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS,
            )
            summary = (response.message.content or "").strip()
            return summary or None
        except Exception as exc:
            excluded_priorities.add(rule.priority)
            logger.bind(
                uid=uid,
                session_id=session_id,
                **channel_log_extra(channel, model_entry),
            ).warning(
                t(
                    "LOG_CONTEXT_SUMMARY_CHANNEL_FAILED",
                    default="Context summary channel failed: {error}",
                    error=format_exception_message(exc),
                )
            )
            selection = await select_channel(
                db,
                chat_channel,
                "CHAT",
                call_context="context_summary_retry",
                excluded_priorities=excluded_priorities,
                cursor_key=cursor_key,
            )

    await _release_db_session(db)
    return None


async def ensure_context_summary(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    before_id: int | None,
    current_message: str,
    context_window_k: int,
    max_tokens: int,
    reserved_tokens: int,
    tools: list[dict] | None = None,
    safety_margin_tokens: int = 256,
) -> ContextSummaryState:
    state = await get_context_summary_state(db, session_id=session_id, uid=uid)
    raw_history = await message_crud.get_history(
        db,
        session_id=session_id,
        uid=uid,
        limit=5000,
        before_id=before_id,
        after_id=state.message_id,
    )
    messages = list(reversed(parse_db_messages_to_internal(raw_history)))
    threshold_percent = cfg.other.context_summary_threshold_percent
    usage = _calc_token_usage(
        messages=messages,
        summary_content=state.content,
        current_message=current_message,
        reserved_tokens=reserved_tokens,
        tools=tools,
        context_window_k=context_window_k,
        max_tokens=max_tokens,
        safety_margin_tokens=safety_margin_tokens,
        threshold_percent=threshold_percent,
    )
    logger.bind(
        uid=uid,
        session_id=session_id,
        context_window_tokens=usage["context_window_tokens"],
        output_tokens=usage["output_tokens"],
        safety_margin_tokens=usage["safety_tokens"],
        input_budget=usage["input_budget"],
        threshold_percent=threshold_percent,
        summary_trigger_tokens=usage["summary_trigger_tokens"],
        compression_goal_tokens=usage["compression_goal_tokens"],
        required_tokens=usage["required_tokens"],
        reserved_tokens=reserved_tokens,
        summary_tokens=usage["summary_tokens"],
        history_tokens=usage["history_tokens"],
        current_message_tokens=usage["current_message_tokens"],
        tools_tokens=usage["tools_tokens"],
        history_message_count=usage["history_message_count"],
    ).debug(
        "Context summary check: required={required_tokens}, trigger={summary_trigger_tokens}, "
        "goal={compression_goal_tokens}, threshold={threshold_percent}%, input_budget={input_budget}, "
        "output={output_tokens}, safety={safety_tokens}, reserved={reserved_tokens}, summary={summary_tokens}, "
        "history={history_tokens}, current={current_message_tokens}, tools={tools_tokens}, "
        "history_messages={history_message_count}",
        required_tokens=usage["required_tokens"],
        summary_trigger_tokens=usage["summary_trigger_tokens"],
        compression_goal_tokens=usage["compression_goal_tokens"],
        threshold_percent=threshold_percent,
        input_budget=usage["input_budget"],
        output_tokens=usage["output_tokens"],
        safety_tokens=usage["safety_tokens"],
        reserved_tokens=reserved_tokens,
        summary_tokens=usage["summary_tokens"],
        history_tokens=usage["history_tokens"],
        current_message_tokens=usage["current_message_tokens"],
        tools_tokens=usage["tools_tokens"],
        history_message_count=usage["history_message_count"],
    )
    if usage["required_tokens"] < usage["summary_trigger_tokens"]:
        logger.bind(uid=uid, session_id=session_id).debug(
            "Context summary skipped: threshold not reached, required={required_tokens}, trigger={summary_trigger_tokens}",
            required_tokens=usage["required_tokens"],
            summary_trigger_tokens=usage["summary_trigger_tokens"],
        )
        await _release_db_session(db)
        return state

    # 进入压缩循环前先提交，避免后续长请求占用调用方事务。
    await _release_db_session(db)

    remaining_messages = list(messages)
    is_first_summary = state.content is None
    original_messages = list(messages)

    while True:
        usage = _calc_token_usage(
            messages=remaining_messages,
            summary_content=state.content,
            current_message=current_message,
            reserved_tokens=reserved_tokens,
            tools=tools,
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            safety_margin_tokens=safety_margin_tokens,
            threshold_percent=threshold_percent,
        )
        logger.bind(
            uid=uid,
            session_id=session_id,
            required_tokens=usage["required_tokens"],
            summary_trigger_tokens=usage["summary_trigger_tokens"],
            compression_goal_tokens=usage["compression_goal_tokens"],
            summary_tokens=usage["summary_tokens"],
            history_tokens=usage["history_tokens"],
            reserved_tokens=reserved_tokens,
            current_message_tokens=usage["current_message_tokens"],
            tools_tokens=usage["tools_tokens"],
            history_message_count=usage["history_message_count"],
        ).debug(
            "Context summary recheck: required={required_tokens}, trigger={summary_trigger_tokens}, "
            "goal={compression_goal_tokens}, reserved={reserved_tokens}, summary={summary_tokens}, "
            "history={history_tokens}, current={current_message_tokens}, tools={tools_tokens}, "
            "history_messages={history_message_count}",
            required_tokens=usage["required_tokens"],
            summary_trigger_tokens=usage["summary_trigger_tokens"],
            compression_goal_tokens=usage["compression_goal_tokens"],
            reserved_tokens=reserved_tokens,
            summary_tokens=usage["summary_tokens"],
            history_tokens=usage["history_tokens"],
            current_message_tokens=usage["current_message_tokens"],
            tools_tokens=usage["tools_tokens"],
            history_message_count=usage["history_message_count"],
        )
        if usage["required_tokens"] <= usage["compression_goal_tokens"]:
            await _release_db_session(db)
            return state

        available_history_tokens = max(
            1,
            usage["input_budget"]
            - reserved_tokens
            - usage["summary_tokens"]
            - usage["current_message_tokens"]
            - usage["tools_tokens"],
        )
        segment_target_tokens = max(1, available_history_tokens // 2)
        segment = _select_summary_segment(remaining_messages, segment_target_tokens)

        if segment and segment[-1].id is not None:
            recent_dialogue = (
                _join_messages(_select_recent_rounds(original_messages, 2))
                if is_first_summary
                else "(none)"
            )
            prompt = CONTEXT_SUMMARY_PROMPT.format(
                existing_summary=state.content or "(none)",
                recent_dialogue=recent_dialogue,
                conversation=_join_messages(segment),
            )
            summary = await _generate_summary_text(
                db,
                profile=profile,
                cfg=cfg,
                prompt=prompt,
                safety_margin_tokens=safety_margin_tokens,
                uid=uid,
                session_id=session_id,
            )
            if not summary:
                await _release_db_session(db)
                return state

            logger.bind(
                uid=uid,
                session_id=session_id,
                summarized_through_message_id=segment[-1].id,
                summarized_message_count=len(segment),
                summary_tokens=estimate_tokens(summary),
            ).debug("Context summary generated:\n{summary}", summary=summary)

            updated = await _persist_context_summary(
                session_id=session_id,
                uid=uid,
                expected_message_id=state.message_id,
                summary=summary,
                message_id=segment[-1].id,
            )
            if not updated:
                return await get_context_summary_state(db, session_id=session_id, uid=uid)

            state = ContextSummaryState(content=summary, message_id=segment[-1].id)
            remaining_messages = _remaining_after_segment(remaining_messages, segment)
            is_first_summary = False
            continue

        if not state.content or state.message_id is None:
            logger.bind(
                uid=uid,
                session_id=session_id,
                history_message_count=len(remaining_messages),
                available_history_tokens=available_history_tokens,
                segment_target_tokens=segment_target_tokens,
            ).debug(
                "Context summary skipped: no complete historical turn can be summarized, history_messages={history_message_count}, available_history_tokens={available_history_tokens}, segment_target_tokens={segment_target_tokens}",
                history_message_count=len(remaining_messages),
                available_history_tokens=available_history_tokens,
                segment_target_tokens=segment_target_tokens,
            )
            await _release_db_session(db)
            return state

        previous_summary_tokens = usage["summary_tokens"]
        compress_prompt = CONTEXT_SUMMARY_COMPRESS_PROMPT.format(summary=state.content)
        compressed = await _generate_summary_text(
            db,
            profile=profile,
            cfg=cfg,
            prompt=compress_prompt,
            safety_margin_tokens=safety_margin_tokens,
            uid=uid,
            session_id=session_id,
        )
        if not compressed:
            await _release_db_session(db)
            return state

        compressed_tokens = _estimate_summary_tokens(compressed)
        if compressed_tokens >= previous_summary_tokens:
            logger.bind(
                uid=uid,
                session_id=session_id,
                previous_summary_tokens=previous_summary_tokens,
                compressed_tokens=compressed_tokens,
                required_tokens=usage["required_tokens"],
                compression_goal_tokens=usage["compression_goal_tokens"],
            ).debug(
                "Context summary compress stopped: no size reduction, previous={previous_summary_tokens}, current={compressed_tokens}, required={required_tokens}, goal={compression_goal_tokens}",
                previous_summary_tokens=previous_summary_tokens,
                compressed_tokens=compressed_tokens,
                required_tokens=usage["required_tokens"],
                compression_goal_tokens=usage["compression_goal_tokens"],
            )
            await _release_db_session(db)
            return state

        logger.bind(
            uid=uid,
            session_id=session_id,
            summarized_through_message_id=state.message_id,
            summary_tokens=estimate_tokens(compressed),
        ).debug("Context summary recompressed:\n{summary}", summary=compressed)

        updated = await _persist_context_summary(
            session_id=session_id,
            uid=uid,
            expected_message_id=state.message_id,
            summary=compressed,
            message_id=state.message_id,
        )
        if not updated:
            return await get_context_summary_state(db, session_id=session_id, uid=uid)
        state = ContextSummaryState(content=compressed, message_id=state.message_id)
