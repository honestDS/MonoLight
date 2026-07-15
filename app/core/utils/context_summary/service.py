from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_CONTEXT_SUMMARY_TRIGGER_PAIR_REQUIRED,
    ERR_CONTEXT_SUMMARY_WORK_INVALID_DURING,
)
from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import CONTEXT_SUMMARY_COMPRESS_PROMPT
from app.core.utils.context_budget import measure_context_request_usage
from app.core.utils.context_messages import message_token_text
from app.core.utils.context_summary.boundary import (
    ContextSummaryTriggerMode,
    resolve_context_summary_boundary,
)
from app.core.utils.context_summary.cleanup import (
    cleanup_context_summary_work_safely,
)
from app.core.utils.context_summary.common import (
    ContextSummaryState,
    ContextSummaryWorkInvalidError,
    ContextSummaryWorkValidityChecker,
    calc_token_usage,
    contains_context_summary_work_invalid,
    ensure_context_summary_work_valid,
    estimate_summary_tokens,
)
from app.core.utils.context_summary.history import (
    measure_complete_replacement_input,
    measure_snapshot_history,
)
from app.core.utils.context_summary.selection import select_context_summary_model
from app.core.utils.context_summary.snapshot import build_context_summary_snapshot
from app.core.utils.context_summary.stage import (
    build_summary_work_identity,
    call_fixed_summary_model,
    generate_snapshot_summary_result,
    release_db_session,
)
from app.core.utils.context_summary.user_message_block import (
    append_covered_user_message,
    split_covered_user_message,
)
from app.core.utils.dispatcher.helpers import format_exception_message
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage
from app.models.profile import Profile, ProfileConfig
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

CONTEXT_SUMMARY_MAX_REFINEMENT_ATTEMPTS = 2


ContextSummaryLifecycleCallback = Callable[[dict[str, object]], Awaitable[None]]


@dataclass
class ContextSummaryLifecycle:
    work_dedupe_key: str | None = None
    event_callback: ContextSummaryLifecycleCallback | None = None
    event_started: bool = False


async def emit_context_summary_lifecycle_event(
    lifecycle: ContextSummaryLifecycle,
    *,
    event_type: str,
) -> bool:
    if lifecycle.event_callback is None:
        return False
    try:
        await lifecycle.event_callback({"type": event_type})
        return True
    except Exception:
        logger.warning(
            "Failed to publish context summary lifecycle event: {event_type}",
            event_type=event_type,
            exc_info=True,
        )
        return False


async def persist_context_summary(
    *,
    session_id: str,
    uid: str,
    expected_message_id: int | None,
    expected_revision: int,
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
            expected_revision=expected_revision,
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
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
) -> str | None:
    excluded_priorities: set[int] = set()
    call_context = "context_summary"
    prompt_tokens = estimate_tokens(prompt)

    while True:
        await ensure_context_summary_work_valid(work_validity_checker)
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
        await ensure_context_summary_work_valid(work_validity_checker)
        try:
            generated = await call_fixed_summary_model(
                model=model,
                prompt=prompt,
                input_tokens=prompt_tokens,
            )
            await ensure_context_summary_work_valid(work_validity_checker)
            return generated
        except Exception as exc:
            if contains_context_summary_work_invalid(exc):
                raise ContextSummaryWorkInvalidError(t(ERR_CONTEXT_SUMMARY_WORK_INVALID_DURING, stage="model execution")) from exc
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


async def _ensure_context_summary(
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
    trigger_mode: ContextSummaryTriggerMode | None = None,
    fixed_upper_message_id: int | None = None,
    fixed_request_messages: list[InternalMessage] | None = None,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
    lifecycle: ContextSummaryLifecycle,
) -> ContextSummaryState:
    await ensure_context_summary_work_valid(work_validity_checker)
    state = await get_context_summary_state(db, session_id=session_id, uid=uid)
    if (trigger_mode is None) != (fixed_upper_message_id is None):
        raise ValueError(t(ERR_CONTEXT_SUMMARY_TRIGGER_PAIR_REQUIRED))

    boundary = None
    if trigger_mode is not None and fixed_upper_message_id is not None:
        boundary = await resolve_context_summary_boundary(
            db,
            session_id=session_id,
            uid=uid,
            expected_summary_message_id=state.message_id,
            trigger_mode=trigger_mode,
            fixed_upper_message_id=fixed_upper_message_id,
        )

    if boundary is not None and boundary.target_message_id is None:
        await release_db_session(db)
        return state

    existing_model_summary, existing_user_message = split_covered_user_message(state.content)
    model_excluded_message_ids = [boundary.covered_user_message_id] if boundary is not None and boundary.trigger_mode == ContextSummaryTriggerMode.TOOL_RESULT and boundary.covered_user_message_id is not None else None
    snapshot = await build_context_summary_snapshot(
        db,
        session_id=session_id,
        uid=uid,
        expected_summary_message_id=state.message_id,
        before_id=before_id,
        frozen_user_message_ids=frozen_user_message_ids,
        target_message_id=boundary.target_message_id if boundary is not None else None,
        model_excluded_message_ids=model_excluded_message_ids,
    )
    history_tokens, history_message_count = await measure_snapshot_history(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
        use_request_token_text=fixed_request_messages is not None,
    )
    await ensure_context_summary_work_valid(work_validity_checker)
    threshold_percent = cfg.other.context_summary_threshold_percent
    request_usage = None
    if fixed_request_messages is not None:
        summary_message = state.as_message()
        summary_tokens = estimate_tokens(message_token_text(summary_message)) if summary_message is not None else 0
        request_usage = measure_context_request_usage(
            messages=fixed_request_messages,
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            tools=tools,
            safety_margin_tokens=safety_margin_tokens,
            threshold_percent=threshold_percent,
            additional_non_system_tokens=history_tokens + summary_tokens,
        )
        usage = {
            "summary_tokens": summary_tokens,
            "history_tokens": history_tokens,
            "tools_tokens": request_usage.budget.tools_tokens,
            "context_window_tokens": request_usage.budget.context_window_tokens,
            "output_tokens": request_usage.budget.output_tokens,
            "safety_tokens": request_usage.budget.safety_margin_tokens,
            "input_budget": request_usage.budget.context_window_tokens - request_usage.budget.output_tokens - request_usage.budget.safety_margin_tokens,
            "current_message_tokens": request_usage.non_system_tokens,
            "required_tokens": request_usage.required_input_tokens,
            "summary_trigger_tokens": request_usage.summary_trigger_tokens,
            "compression_goal_tokens": request_usage.summary_trigger_tokens,
            "reserved_tokens": request_usage.system_tokens,
            "threshold_percent": threshold_percent,
            "history_message_count": history_message_count,
        }
    else:
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
    threshold_reached = usage["required_tokens"] >= usage["summary_trigger_tokens"]
    check_result = "triggered" if threshold_reached else "skipped"
    logger.bind(
        uid=uid,
        session_id=session_id,
        check_result=check_result,
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
        "Context summary check: result={check_result}, required={required_tokens}, trigger={summary_trigger_tokens}, "
        "goal={compression_goal_tokens}, threshold={threshold_percent}%, input_budget={input_budget}, "
        "output={output_tokens}, safety={safety_tokens}, reserved={reserved_tokens}, summary={summary_tokens}, "
        "history={history_tokens}, current={current_message_tokens}, tools={tools_tokens}, "
        "history_messages={history_message_count}",
        check_result=check_result,
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
    if not threshold_reached:
        await release_db_session(db)
        return state

    if not snapshot.has_persistent_history and (not state.content or state.message_id is None):
        await release_db_session(db)
        return state

    lifecycle.event_started = await emit_context_summary_lifecycle_event(
        lifecycle,
        event_type="context_summary_start",
    )

    candidate_summary = existing_model_summary
    summarized_message_count = 0
    completed_stage = None
    if snapshot.has_persistent_history:
        _, lifecycle.work_dedupe_key, _ = build_summary_work_identity(
            session_id=session_id,
            uid=uid,
            snapshot=snapshot,
            revision=state.revision,
        )
        generated = await generate_snapshot_summary_result(
            db,
            session_id=session_id,
            uid=uid,
            profile=profile,
            cfg=cfg,
            snapshot=snapshot,
            existing_summary=existing_model_summary,
            existing_summary_revision=state.revision,
            safety_margin_tokens=safety_margin_tokens,
            work_validity_checker=work_validity_checker,
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
        if fixed_request_messages is not None:
            candidate_message_id = snapshot.persistent_summary_target_id or state.message_id
            candidate_summary_message = ContextSummaryState(
                content=candidate_summary,
                message_id=candidate_message_id,
            ).as_message()
            candidate_request_messages = [
                *fixed_request_messages,
                *([candidate_summary_message] if candidate_summary_message is not None else []),
                *recent_messages,
            ]
            measured_final_usage = measure_context_request_usage(
                messages=candidate_request_messages,
                context_window_k=context_window_k,
                max_tokens=max_tokens,
                tools=tools,
                safety_margin_tokens=safety_margin_tokens,
                threshold_percent=threshold_percent,
            )
            final_usage = {
                "required_tokens": measured_final_usage.required_input_tokens,
                "compression_goal_tokens": measured_final_usage.summary_trigger_tokens,
                "summary_tokens": estimate_tokens(message_token_text(candidate_summary_message)) if candidate_summary_message is not None else 0,
            }
        else:
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
        await ensure_context_summary_work_valid(work_validity_checker)
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
                    work_validity_checker=work_validity_checker,
                )
            except Exception as exc:
                if contains_context_summary_work_invalid(exc):
                    raise ContextSummaryWorkInvalidError(t(ERR_CONTEXT_SUMMARY_WORK_INVALID_DURING, stage="refinement")) from exc
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
                work_validity_checker=work_validity_checker,
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
    if boundary is not None and target_message_id != boundary.target_message_id:
        await release_db_session(db)
        return state

    replacement_input_tokens = None
    if boundary is not None:
        replacement_input_tokens = await measure_complete_replacement_input(
            db,
            session_id=session_id,
            uid=uid,
            snapshot=snapshot,
            existing_summary=state.content,
        )
    if boundary is not None and boundary.trigger_mode == ContextSummaryTriggerMode.TOOL_RESULT:
        candidate_summary = append_covered_user_message(
            candidate_summary,
            message_id=boundary.covered_user_message_id,
            content=boundary.covered_user_message_content,
        )
    else:
        candidate_summary = append_covered_user_message(
            candidate_summary,
            message_id=existing_user_message.message_id if existing_user_message is not None else None,
            content=existing_user_message.content if existing_user_message is not None else None,
        )
    if not candidate_summary:
        await release_db_session(db)
        return state
    if replacement_input_tokens is not None and estimate_tokens(candidate_summary) >= replacement_input_tokens:
        logger.bind(
            uid=uid,
            session_id=session_id,
            replacement_input_tokens=replacement_input_tokens,
            candidate_tokens=estimate_tokens(candidate_summary),
        ).warning("Context summary candidate did not reduce its complete replacement input")
        await release_db_session(db)
        return state

    logger.bind(
        uid=uid,
        session_id=session_id,
        summarized_through_message_id=target_message_id,
        summarized_message_count=summarized_message_count,
        summary_tokens=estimate_tokens(candidate_summary),
    ).debug("Context summary generated:\n{summary}", summary=candidate_summary)

    await ensure_context_summary_work_valid(work_validity_checker)
    updated = await persist_context_summary(
        session_id=session_id,
        uid=uid,
        expected_message_id=state.message_id,
        expected_revision=state.revision,
        summary=candidate_summary,
        message_id=target_message_id,
    )
    if not updated:
        return await get_context_summary_state(db, session_id=session_id, uid=uid)
    return ContextSummaryState(
        content=candidate_summary,
        message_id=target_message_id,
        revision=state.revision + 1,
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
    trigger_mode: ContextSummaryTriggerMode | None = None,
    fixed_upper_message_id: int | None = None,
    fixed_request_messages: list[InternalMessage] | None = None,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
    lifecycle_event_callback: ContextSummaryLifecycleCallback | None = None,
) -> ContextSummaryState:
    lifecycle = ContextSummaryLifecycle(event_callback=lifecycle_event_callback)
    try:
        return await _ensure_context_summary(
            db,
            session_id=session_id,
            uid=uid,
            profile=profile,
            cfg=cfg,
            before_id=before_id,
            current_message=current_message,
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            reserved_tokens=reserved_tokens,
            tools=tools,
            safety_margin_tokens=safety_margin_tokens,
            frozen_user_message_ids=frozen_user_message_ids,
            trigger_mode=trigger_mode,
            fixed_upper_message_id=fixed_upper_message_id,
            fixed_request_messages=fixed_request_messages,
            work_validity_checker=work_validity_checker,
            lifecycle=lifecycle,
        )
    finally:
        if lifecycle.event_started:
            await emit_context_summary_lifecycle_event(
                lifecycle,
                event_type="context_summary_end",
            )
        if lifecycle.work_dedupe_key is not None:
            await cleanup_context_summary_work_safely(
                lifecycle.work_dedupe_key,
            )
