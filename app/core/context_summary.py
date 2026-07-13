import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context_summary_call import (
    CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS as CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS,
)
from app.core.context_summary_call import call_context_summary_model
from app.core.context_summary_selection import select_context_summary_model
from app.core.context_summary_snapshot import (
    ContextSummarySnapshot,
    build_context_summary_snapshot,
    iter_persistent_summary_rounds,
)
from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import (
    CONTEXT_SUMMARY_COMPRESS_PROMPT,
    CONTEXT_SUMMARY_PROMPT,
    CONTEXT_SUMMARY_WRAPPER,
)
from app.core.utils.dispatcher.helpers import format_exception_message
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole
from app.models.profile import Profile, ProfileConfig
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


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
    history_tokens_override: int | None = None,
    history_message_count_override: int | None = None,
) -> dict[str, int]:
    summary_tokens = _estimate_summary_tokens(summary_content)
    history_tokens = history_tokens_override if history_tokens_override is not None else sum(estimate_tokens(_serialize_message(message)) for message in messages)
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
        "history_message_count": history_message_count_override if history_message_count_override is not None else len(messages),
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
    summary_channel = cfg.channel.context_summary_channel
    excluded_priorities: set[int] = set()
    call_context = "context_summary"
    prompt_tokens = estimate_tokens(prompt)

    while True:
        model = await select_context_summary_model(
            db,
            profile_id=profile.id,
            channel_config=summary_channel,
            safety_margin_tokens=safety_margin_tokens,
            excluded_priorities=excluded_priorities,
            call_context=call_context,
        )
        if model is None:
            await _release_db_session(db)
            return None

        if not model.accepts_prompt_tokens(prompt_tokens):
            excluded_priorities.add(model.priority)
            call_context = "context_summary_retry"
            continue

        # 调模型前释放调用方事务，避免长请求期间占着写锁。
        await _release_db_session(db)

        try:
            return await call_context_summary_model(model=model, prompt=prompt)
        except Exception as exc:
            excluded_priorities.add(model.priority)
            call_context = "context_summary_retry"
            logger.bind(
                uid=uid,
                session_id=session_id,
                channel_id=model.channel_id,
                channel_name=model.channel_name,
                model_id=model.model_id,
                model_name=model.model_id,
            ).warning(
                t(
                    "LOG_CONTEXT_SUMMARY_CHANNEL_FAILED",
                    default="Context summary channel failed: {error}",
                    error=format_exception_message(exc),
                )
            )


async def _measure_snapshot_history(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
) -> tuple[int, int]:
    history_tokens = sum(estimate_tokens(_serialize_message(message)) for message in snapshot.recent_messages)
    history_message_count = len(snapshot.recent_messages)
    async for round_messages in iter_persistent_summary_rounds(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    ):
        history_tokens += sum(estimate_tokens(_serialize_message(message)) for message in round_messages)
        history_message_count += len(round_messages)
    return history_tokens, history_message_count


async def _generate_snapshot_summary(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    snapshot: ContextSummarySnapshot,
    existing_summary: str | None,
    segment_target_tokens: int,
    safety_margin_tokens: int,
) -> tuple[str | None, int]:
    candidate_summary = existing_summary
    pending_messages: list[InternalMessage] = []
    pending_tokens = 0
    summarized_message_count = 0
    summarized_through_message_id = snapshot.expected_summary_message_id or 0

    async def flush_pending() -> bool:
        nonlocal candidate_summary, pending_messages, pending_tokens
        if not pending_messages:
            return True
        prompt = CONTEXT_SUMMARY_PROMPT.format(
            existing_summary=candidate_summary or "(none)",
            recent_dialogue="(none)",
            conversation=_join_messages(pending_messages),
        )
        generated = await _generate_summary_text(
            db,
            profile=profile,
            cfg=cfg,
            prompt=prompt,
            safety_margin_tokens=safety_margin_tokens,
            uid=uid,
            session_id=session_id,
        )
        if not generated:
            return False
        candidate_summary = generated
        pending_messages = []
        pending_tokens = 0
        return True

    async for round_messages in iter_persistent_summary_rounds(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    ):
        round_tokens = sum(estimate_tokens(_serialize_message(message)) for message in round_messages)
        if pending_messages and pending_tokens + round_tokens > segment_target_tokens:
            if not await flush_pending():
                return None, summarized_message_count
        if round_tokens > segment_target_tokens:
            logger.bind(
                uid=uid,
                session_id=session_id,
                round_start_message_id=round_messages[0].id,
                round_end_message_id=round_messages[-1].id,
                round_tokens=round_tokens,
                segment_target_tokens=segment_target_tokens,
            ).debug(
                "Context summary stopped: one historical round exceeds the current segment target, round_start={round_start_message_id}, round_end={round_end_message_id}, round_tokens={round_tokens}, target={segment_target_tokens}",
                round_start_message_id=round_messages[0].id,
                round_end_message_id=round_messages[-1].id,
                round_tokens=round_tokens,
                segment_target_tokens=segment_target_tokens,
            )
            return None, summarized_message_count
        pending_messages.extend(round_messages)
        pending_tokens += round_tokens
        summarized_message_count += len(round_messages)
        if round_messages[-1].id is not None:
            summarized_through_message_id = round_messages[-1].id

    if not await flush_pending():
        return None, summarized_message_count
    if summarized_through_message_id != snapshot.persistent_summary_target_id:
        return None, summarized_message_count
    return candidate_summary, summarized_message_count


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
    frozen_user_message_ids: list[int] | None = None,
) -> ContextSummaryState:
    state = await get_context_summary_state(db, session_id=session_id, uid=uid)
    snapshot = await build_context_summary_snapshot(
        db,
        session_id=session_id,
        uid=uid,
        expected_summary_message_id=state.message_id,
        before_id=before_id,
        frozen_user_message_ids=frozen_user_message_ids,
    )
    history_tokens, history_message_count = await _measure_snapshot_history(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    )
    threshold_percent = cfg.other.context_summary_threshold_percent
    usage = _calc_token_usage(
        messages=[],
        summary_content=state.content,
        current_message=current_message,
        reserved_tokens=reserved_tokens,
        tools=tools,
        context_window_k=context_window_k,
        max_tokens=max_tokens,
        safety_margin_tokens=safety_margin_tokens,
        threshold_percent=threshold_percent,
        history_tokens_override=history_tokens,
        history_message_count_override=history_message_count,
    )
    logger.bind(
        uid=uid,
        session_id=session_id,
        expected_summary_message_id=snapshot.expected_summary_message_id,
        snapshot_before_id=snapshot.snapshot_before_id,
        snapshot_max_message_id=snapshot.snapshot_max_message_id,
        persistent_summary_target_id=snapshot.persistent_summary_target_id,
        recent_round_start_ids=snapshot.recent_round_start_ids,
        frozen_user_message_ids=snapshot.frozen_user_message_ids,
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

    candidate_summary = state.content
    summarized_message_count = 0
    if snapshot.has_persistent_history:
        available_history_tokens = max(
            1,
            usage["input_budget"] - reserved_tokens - usage["summary_tokens"] - usage["current_message_tokens"] - usage["tools_tokens"],
        )
        segment_target_tokens = max(1, available_history_tokens // 2)
        candidate_summary, summarized_message_count = await _generate_snapshot_summary(
            db,
            session_id=session_id,
            uid=uid,
            profile=profile,
            cfg=cfg,
            snapshot=snapshot,
            existing_summary=state.content,
            segment_target_tokens=segment_target_tokens,
            safety_margin_tokens=safety_margin_tokens,
        )
        if not candidate_summary:
            await _release_db_session(db)
            return state
    elif not candidate_summary or state.message_id is None:
        await _release_db_session(db)
        return state

    recent_messages = list(snapshot.recent_messages)
    while True:
        final_usage = _calc_token_usage(
            messages=recent_messages,
            summary_content=candidate_summary,
            current_message=current_message,
            reserved_tokens=reserved_tokens,
            tools=tools,
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            safety_margin_tokens=safety_margin_tokens,
            threshold_percent=threshold_percent,
        )
        if final_usage["required_tokens"] <= final_usage["compression_goal_tokens"]:
            break

        previous_summary_tokens = final_usage["summary_tokens"]
        compressed = await _generate_summary_text(
            db,
            profile=profile,
            cfg=cfg,
            prompt=CONTEXT_SUMMARY_COMPRESS_PROMPT.format(summary=candidate_summary),
            safety_margin_tokens=safety_margin_tokens,
            uid=uid,
            session_id=session_id,
        )
        if not compressed:
            await _release_db_session(db)
            return state
        compressed_tokens = _estimate_summary_tokens(compressed)
        if compressed_tokens >= previous_summary_tokens:
            break
        candidate_summary = compressed

    target_message_id = snapshot.persistent_summary_target_id if snapshot.has_persistent_history else state.message_id
    if target_message_id is None:
        await _release_db_session(db)
        return state

    logger.bind(
        uid=uid,
        session_id=session_id,
        summarized_through_message_id=target_message_id,
        summarized_message_count=summarized_message_count,
        summary_tokens=estimate_tokens(candidate_summary),
    ).debug("Context summary generated:\n{summary}", summary=candidate_summary)

    updated = await _persist_context_summary(
        session_id=session_id,
        uid=uid,
        expected_message_id=state.message_id,
        summary=candidate_summary,
        message_id=target_message_id,
    )
    if not updated:
        return await get_context_summary_state(db, session_id=session_id, uid=uid)
    return ContextSummaryState(content=candidate_summary, message_id=target_message_id)
