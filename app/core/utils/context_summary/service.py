from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import CONTEXT_SUMMARY_COMPRESS_PROMPT
from app.core.utils.context_summary.common import (
    ContextSummaryState,
    calc_token_usage,
    estimate_summary_tokens,
)
from app.core.utils.context_summary.history import measure_snapshot_history
from app.core.utils.context_summary.selection import select_context_summary_model
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot
from app.core.utils.context_summary.stage import (
    call_fixed_summary_model,
    generate_snapshot_summary_result,
    release_db_session,
)
from app.core.utils.dispatcher.helpers import format_exception_message
from app.core.utils.tokenizer import estimate_tokens
from app.models.profile import Profile, ProfileConfig
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

CONTEXT_SUMMARY_MAX_REFINEMENT_ATTEMPTS = 2


async def persist_context_summary(
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
        revision=session.context_summary_revision,
    )


async def generate_summary_text(
    db: AsyncSession,
    *,
    profile: Profile,
    cfg: ProfileConfig,
    prompt: str,
    safety_margin_tokens: int,
    uid: str,
    session_id: str,
) -> str | None:
    excluded_priorities: set[int] = set()
    call_context = "context_summary"
    prompt_tokens = estimate_tokens(prompt)

    while True:
        model = await select_context_summary_model(
            db,
            profile_id=profile.id,
            channel_config=cfg.channel.context_summary_channel,
            safety_margin_tokens=safety_margin_tokens,
            excluded_priorities=set(excluded_priorities),
            call_context=call_context,
        )
        if model is None:
            await release_db_session(db)
            return None

        if not model.accepts_prompt_tokens(prompt_tokens):
            excluded_priorities.add(model.priority)
            call_context = "context_summary_retry"
            continue

        await release_db_session(db)
        try:
            return await call_fixed_summary_model(
                model=model,
                prompt=prompt,
                input_tokens=prompt_tokens,
            )
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
    history_tokens, history_message_count = await measure_snapshot_history(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    )
    threshold_percent = cfg.other.context_summary_threshold_percent
    usage = calc_token_usage(
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
        await release_db_session(db)
        return state

    candidate_summary = state.content
    summarized_message_count = 0
    completed_stage = None
    if snapshot.has_persistent_history:
        generated = await generate_snapshot_summary_result(
            db,
            session_id=session_id,
            uid=uid,
            profile=profile,
            cfg=cfg,
            snapshot=snapshot,
            existing_summary=state.content,
            existing_summary_revision=state.revision,
            safety_margin_tokens=safety_margin_tokens,
        )
        candidate_summary = generated.content
        summarized_message_count = generated.message_count
        completed_stage = generated.completed_stage
        if not candidate_summary or completed_stage is None:
            await release_db_session(db)
            return state
    elif not candidate_summary or state.message_id is None:
        await release_db_session(db)
        return state

    recent_messages = list(snapshot.recent_messages)
    refinement_attempts = 0
    while True:
        final_usage = calc_token_usage(
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
        if refinement_attempts >= CONTEXT_SUMMARY_MAX_REFINEMENT_ATTEMPTS:
            logger.bind(
                uid=uid,
                session_id=session_id,
                refinement_attempts=refinement_attempts,
                required_tokens=final_usage["required_tokens"],
                compression_goal_tokens=final_usage["compression_goal_tokens"],
            ).warning("Context summary refinement limit reached before compression goal")
            break

        refinement_attempts += 1
        previous_summary_tokens = final_usage["summary_tokens"]
        if completed_stage is not None:
            from app.core.utils.context_summary.reduction import (
                refine_completed_summary_stage,
            )

            try:
                refined = await refine_completed_summary_stage(
                    db,
                    profile=profile,
                    cfg=cfg,
                    lower_stage=completed_stage,
                    safety_margin_tokens=safety_margin_tokens,
                    refinement_index=refinement_attempts,
                )
            except Exception as exc:
                logger.bind(
                    uid=uid,
                    session_id=session_id,
                    refinement_attempts=refinement_attempts,
                ).warning(
                    "Context summary refinement stage failed: {error}",
                    error=format_exception_message(exc),
                )
                await release_db_session(db)
                return state
            compressed = refined.content
            completed_stage = refined.stage
        else:
            compressed = await generate_summary_text(
                db,
                profile=profile,
                cfg=cfg,
                prompt=CONTEXT_SUMMARY_COMPRESS_PROMPT.format(summary=candidate_summary),
                safety_margin_tokens=safety_margin_tokens,
                uid=uid,
                session_id=session_id,
            )
            if not compressed:
                await release_db_session(db)
                return state
        compressed_tokens = estimate_summary_tokens(compressed)
        if compressed_tokens >= previous_summary_tokens:
            break
        candidate_summary = compressed

    target_message_id = snapshot.persistent_summary_target_id if snapshot.has_persistent_history else state.message_id
    if target_message_id is None:
        await release_db_session(db)
        return state

    logger.bind(
        uid=uid,
        session_id=session_id,
        summarized_through_message_id=target_message_id,
        summarized_message_count=summarized_message_count,
        summary_tokens=estimate_tokens(candidate_summary),
    ).debug("Context summary generated:\n{summary}", summary=candidate_summary)

    updated = await persist_context_summary(
        session_id=session_id,
        uid=uid,
        expected_message_id=state.message_id,
        summary=candidate_summary,
        message_id=target_message_id,
    )
    if not updated:
        return await get_context_summary_state(db, session_id=session_id, uid=uid)
    return ContextSummaryState(
        content=candidate_summary,
        message_id=target_message_id,
        revision=state.revision,
    )
