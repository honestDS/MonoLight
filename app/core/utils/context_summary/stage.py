import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_CONTEXT_SUMMARY_FRAGMENT_RESULT_CONFLICT,
    ERR_CONTEXT_SUMMARY_FRAGMENT_WRITE_ORDER_FAILED,
    ERR_CONTEXT_SUMMARY_LAYER_CONFLICT,
    ERR_CONTEXT_SUMMARY_LAYER_FAILED,
    ERR_CONTEXT_SUMMARY_LAYER_UNSPLITTABLE,
    ERR_CONTEXT_SUMMARY_MODEL_NO_INPUT_BUDGET,
    ERR_CONTEXT_SUMMARY_MODEL_NOT_REDUCED,
    ERR_CONTEXT_SUMMARY_MODEL_RESULT_EMPTY,
    ERR_CONTEXT_SUMMARY_MODEL_RETRIES_EXHAUSTED,
    ERR_CONTEXT_SUMMARY_SNAPSHOT_RANGE_MISSING,
    ERR_CONTEXT_SUMMARY_STAGE_COMPLETION_FAILED,
    ERR_CONTEXT_SUMMARY_STAGE_INPUT_OVER_WINDOW,
    ERR_CONTEXT_SUMMARY_WORK_INVALID_DURING,
)
from app.core.crud.context_summary.fragment import (
    build_context_summary_fragment_dedupe_key,
    context_summary_fragment_crud,
)
from app.core.crud.context_summary.stage import context_summary_stage_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import CONTEXT_SUMMARY_PROMPT
from app.core.utils.context_summary.common import (
    ContextSummaryWorkInvalidError,
    ContextSummaryWorkValidityChecker,
    contains_context_summary_work_invalid,
    ensure_context_summary_work_valid,
)
from app.core.utils.context_summary.history import (
    count_summary_fragments,
    iter_summary_fragments,
    measure_persistent_history,
)
from app.core.utils.context_summary.model_call import call_context_summary_model
from app.core.utils.context_summary.pipeline import (
    SummaryFragmentInput,
    SummaryFragmentResult,
    balanced_fragment_target_tokens,
    run_bounded_fragment_pipeline,
)
from app.core.utils.context_summary.selection import (
    ContextSummaryModelSnapshot,
    select_context_summary_model,
)
from app.core.utils.context_summary.snapshot import ContextSummarySnapshot
from app.core.utils.tokenizer import estimate_tokens
from app.models.context_summary_stage import (
    ContextSummaryFragment,
    ContextSummaryStage,
    ContextSummaryStageStatus,
)
from app.models.profile import Profile, ProfileConfig
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

CONTEXT_SUMMARY_MODEL_ATTEMPTS = 2


@dataclass(frozen=True)
class GeneratedSummaryResult:
    content: str | None
    message_count: int
    completed_stage: ContextSummaryStage | None = None


class ContextSummaryLayerError(RuntimeError):
    pass


def format_context_summary_exception(exc: BaseException) -> str:
    details: list[str] = []
    seen: set[int] = set()

    def collect(current: BaseException) -> None:
        current_id = id(current)
        if current_id in seen:
            return
        seen.add(current_id)

        if isinstance(current, BaseExceptionGroup):
            for nested in current.exceptions:
                collect(nested)
            return

        detail = f"{type(current).__name__}: {current}"
        if detail not in details:
            details.append(detail)

        cause = current.__cause__
        if cause is not None:
            collect(cause)
        elif current.__context__ is not None and not current.__suppress_context__:
            collect(current.__context__)

    collect(exc)
    return " | ".join(details) or f"{type(exc).__name__}: {exc}"


async def release_db_session(db: AsyncSession) -> None:
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


async def call_fixed_summary_model(
    *,
    model: ContextSummaryModelSnapshot,
    prompt: str,
    input_tokens: int,
) -> str:
    last_error: Exception | None = None
    prompt_tokens = estimate_tokens(prompt)
    for attempt in range(1, CONTEXT_SUMMARY_MODEL_ATTEMPTS + 1):
        output_tokens: int | None = None
        try:
            generated = await call_context_summary_model(model=model, prompt=prompt)
            if not generated:
                raise RuntimeError(t(ERR_CONTEXT_SUMMARY_MODEL_RESULT_EMPTY))
            output_tokens = estimate_tokens(generated)
            if output_tokens >= input_tokens:
                raise RuntimeError(
                    t(
                        ERR_CONTEXT_SUMMARY_MODEL_NOT_REDUCED,
                        output_tokens=output_tokens,
                        input_tokens=input_tokens,
                    )
                )
            return generated
        except Exception as exc:
            last_error = exc
            logger.bind(
                channel_id=model.channel_id,
                channel_name=model.channel_name,
                model_id=model.model_id,
                model_priority=model.priority,
                attempt=attempt,
                max_attempts=CONTEXT_SUMMARY_MODEL_ATTEMPTS,
                prompt_tokens=prompt_tokens,
                replacement_input_tokens=input_tokens,
                output_tokens=output_tokens,
                exception_type=type(exc).__name__,
                exception_detail=format_context_summary_exception(exc),
            ).warning("Context summary fixed-model attempt failed")

    detail = format_context_summary_exception(last_error) if last_error is not None else "unknown error"
    raise RuntimeError(t(ERR_CONTEXT_SUMMARY_MODEL_RETRIES_EXHAUSTED, detail=detail)) from last_error


def fragment_prompt(fragment: SummaryFragmentInput) -> str:
    return CONTEXT_SUMMARY_PROMPT.format(
        existing_summary=fragment.existing_summary or "(none)",
        recent_dialogue="(none)",
        conversation=fragment.content,
    )


def fragment_replacement_input_tokens(fragment: SummaryFragmentInput) -> int:
    return fragment.token_count + estimate_tokens(fragment.existing_summary or "")


def build_summary_work_identity(
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
    revision: int,
) -> tuple[int, str, str]:
    snapshot_payload = json.dumps(
        {
            "uid": uid,
            "session_id": session_id,
            "expected_summary_message_id": snapshot.expected_summary_message_id,
            "revision": revision,
            "content_revision": snapshot.content_revision,
            "snapshot_max_message_id": snapshot.snapshot_max_message_id,
            "persistent_summary_target_id": snapshot.persistent_summary_target_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    snapshot_key = hashlib.sha256(snapshot_payload.encode("utf-8")).hexdigest()
    return (
        int(snapshot_key[:15], 16) or 1,
        f"context-summary:{snapshot_key}",
        snapshot_key,
    )


def build_stage_identity(
    *,
    session_id: str,
    uid: str,
    snapshot: ContextSummarySnapshot,
    revision: int,
    model: ContextSummaryModelSnapshot,
    expected_fragment_count: int,
) -> tuple[int, str, str, str, str]:
    work_id, work_dedupe_key, snapshot_key = build_summary_work_identity(
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
        revision=revision,
    )
    model_payload = json.dumps(
        {
            "channel_id": model.channel_id,
            "model_id": model.model_id,
            "priority": model.priority,
            "context_window_tokens": model.context_window_tokens,
            "max_output_tokens": model.max_output_tokens,
            "temperature": model.temperature,
            "top_p": model.top_p,
            "safety_margin_tokens": model.safety_margin_tokens,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    model_key = hashlib.sha256(model_payload.encode("utf-8")).hexdigest()
    stage_payload = f"{snapshot_key}:{model_key}:raw:{expected_fragment_count}"
    stage_key = hashlib.sha256(stage_payload.encode("utf-8")).hexdigest()
    return work_id, work_dedupe_key, snapshot_key, stage_key, model_key


async def write_summary_fragment(
    *,
    stage: ContextSummaryStage,
    result: SummaryFragmentResult,
) -> None:
    fragment = ContextSummaryFragment(
        dedupe_key=build_context_summary_fragment_dedupe_key(
            work_dedupe_key=stage.work_dedupe_key,
            stage_key=stage.stage_key,
            model_key=stage.model_key,
            fragment_index=result.fragment_index,
        ),
        uid=stage.uid,
        session_id=stage.session_id,
        work_id=stage.work_id,
        work_dedupe_key=stage.work_dedupe_key,
        snapshot_key=stage.snapshot_key,
        stage_key=stage.stage_key,
        model_key=stage.model_key,
        fragment_index=result.fragment_index,
        message_start_id=result.message_start_id,
        message_end_id=result.message_end_id,
        channel_id=stage.channel_id,
        model_id=stage.model_id,
        token_count=result.token_count,
        content=result.content,
    )
    async with AsyncSessionLocal() as fragment_db:
        persisted, created = await context_summary_fragment_crud.write_ordered(
            fragment_db,
            fragment=fragment,
        )
    if persisted is None:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FRAGMENT_WRITE_ORDER_FAILED))
    if not created and (persisted.message_start_id != result.message_start_id or persisted.message_end_id != result.message_end_id or persisted.content != result.content):
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FRAGMENT_RESULT_CONFLICT))


async def mark_summary_stage_completed(*, stage: ContextSummaryStage) -> bool:
    async with AsyncSessionLocal() as stage_db:
        return await context_summary_stage_crud.mark_completed(
            stage_db,
            work_dedupe_key=stage.work_dedupe_key,
            stage_key=stage.stage_key,
            model_key=stage.model_key,
        )


async def mark_summary_stage_failed(
    *,
    stage: ContextSummaryStage,
    error: str,
) -> None:
    async with AsyncSessionLocal() as stage_db:
        await context_summary_stage_crud.mark_failed(
            stage_db,
            work_dedupe_key=stage.work_dedupe_key,
            stage_key=stage.stage_key,
            model_key=stage.model_key,
            error=error,
        )


async def invalidate_summary_stage(*, stage: ContextSummaryStage) -> bool:
    async with AsyncSessionLocal() as stage_db:
        return await context_summary_stage_crud.invalidate(
            stage_db,
            work_dedupe_key=stage.work_dedupe_key,
            stage_key=stage.stage_key,
            model_key=stage.model_key,
        )


async def generate_snapshot_summary_with_model(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    snapshot: ContextSummarySnapshot,
    existing_summary: str | None,
    existing_summary_revision: int,
    safety_margin_tokens: int,
    model: ContextSummaryModelSnapshot,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
) -> GeneratedSummaryResult:
    await ensure_context_summary_work_valid(work_validity_checker)
    total_tokens, summarized_message_count = await measure_persistent_history(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
    )
    if total_tokens <= 0:
        return GeneratedSummaryResult(
            content=None,
            message_count=summarized_message_count,
        )

    empty_prompt = CONTEXT_SUMMARY_PROMPT.format(
        existing_summary=existing_summary or "(none)",
        recent_dialogue="(none)",
        conversation="(none)",
    )
    prompt_overhead_tokens = estimate_tokens(empty_prompt)
    max_fragment_tokens = model.input_budget_tokens - prompt_overhead_tokens - 32
    if max_fragment_tokens <= 0:
        raise ContextSummaryLayerError(t(ERR_CONTEXT_SUMMARY_MODEL_NO_INPUT_BUDGET, stage="fragment"))

    fragment_target_tokens = balanced_fragment_target_tokens(
        total_tokens,
        max_fragment_tokens,
    )
    expected_fragment_count = await count_summary_fragments(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
        fragment_target_tokens=fragment_target_tokens,
        max_fragment_tokens=max_fragment_tokens,
    )
    if expected_fragment_count <= 0:
        raise ContextSummaryLayerError(t(ERR_CONTEXT_SUMMARY_LAYER_UNSPLITTABLE))

    snapshot_max_message_id = snapshot.snapshot_max_message_id
    persistent_summary_target_id = snapshot.persistent_summary_target_id
    if snapshot_max_message_id is None or persistent_summary_target_id is None:
        raise ContextSummaryLayerError(t(ERR_CONTEXT_SUMMARY_SNAPSHOT_RANGE_MISSING))

    work_id, work_dedupe_key, snapshot_key, stage_key, model_key = build_stage_identity(
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
        revision=existing_summary_revision,
        model=model,
        expected_fragment_count=expected_fragment_count,
    )
    stage = ContextSummaryStage(
        uid=uid,
        session_id=session_id,
        work_id=work_id,
        work_dedupe_key=work_dedupe_key,
        snapshot_key=snapshot_key,
        stage_key=stage_key,
        lower_stage_key=None,
        model_key=model_key,
        channel_id=model.channel_id,
        model_id=model.model_id,
        context_window_k=max(1, model.context_window_tokens // CONTEXT_WINDOW_TOKENS_PER_K),
        max_output_tokens=model.max_output_tokens,
        safety_margin_tokens=model.safety_margin_tokens,
        expected_summary_message_id=snapshot.expected_summary_message_id,
        expected_summary_revision=existing_summary_revision,
        expected_content_revision=snapshot.content_revision,
        snapshot_max_message_id=snapshot_max_message_id,
        persistent_summary_target_id=persistent_summary_target_id,
        expected_fragment_count=expected_fragment_count,
    )
    await release_db_session(db)
    await ensure_context_summary_work_valid(work_validity_checker)
    async with AsyncSessionLocal() as stage_db:
        persisted_stage, _ = await context_summary_stage_crud.create_stage(stage_db, stage=stage)
    if (
        persisted_stage.model_key != model_key
        or persisted_stage.expected_fragment_count != expected_fragment_count
        or persisted_stage.status
        not in {
            ContextSummaryStageStatus.RUNNING,
            ContextSummaryStageStatus.COMPLETED,
        }
        or persisted_stage.succeeded_fragment_count > expected_fragment_count
        or (persisted_stage.status == ContextSummaryStageStatus.COMPLETED and persisted_stage.succeeded_fragment_count != expected_fragment_count)
    ):
        raise ContextSummaryLayerError(t(ERR_CONTEXT_SUMMARY_LAYER_CONFLICT))

    first_fragment_index = persisted_stage.succeeded_fragment_count
    if persisted_stage.status == ContextSummaryStageStatus.COMPLETED:
        from app.core.utils.context_summary.reduction import (
            reduce_completed_summary_stage_result,
        )

        reduced = await reduce_completed_summary_stage_result(
            db,
            profile=profile,
            cfg=cfg,
            initial_stage=persisted_stage,
            safety_margin_tokens=safety_margin_tokens,
            work_validity_checker=work_validity_checker,
        )
        return GeneratedSummaryResult(
            content=reduced.content,
            message_count=summarized_message_count,
            completed_stage=reduced.stage,
        )

    if first_fragment_index == expected_fragment_count:
        await ensure_context_summary_work_valid(work_validity_checker)
        if await mark_summary_stage_completed(stage=persisted_stage):
            from app.core.utils.context_summary.reduction import (
                reduce_completed_summary_stage_result,
            )

            reduced = await reduce_completed_summary_stage_result(
                db,
                profile=profile,
                cfg=cfg,
                initial_stage=persisted_stage,
                safety_margin_tokens=safety_margin_tokens,
                work_validity_checker=work_validity_checker,
            )
            return GeneratedSummaryResult(
                content=reduced.content,
                message_count=summarized_message_count,
                completed_stage=reduced.stage,
            )
        error = t(ERR_CONTEXT_SUMMARY_STAGE_COMPLETION_FAILED, stage="fragment")
        await mark_summary_stage_failed(stage=persisted_stage, error=error)
        await invalidate_summary_stage(stage=persisted_stage)
        raise ContextSummaryLayerError(error)

    fragments = iter_summary_fragments(
        db,
        session_id=session_id,
        uid=uid,
        snapshot=snapshot,
        existing_summary=existing_summary,
        fragment_target_tokens=fragment_target_tokens,
        max_fragment_tokens=max_fragment_tokens,
        first_fragment_index=first_fragment_index,
    )

    async def process_fragment(fragment: SummaryFragmentInput) -> SummaryFragmentResult:
        await ensure_context_summary_work_valid(work_validity_checker)
        prompt = fragment_prompt(fragment)
        if not model.accepts_prompt_tokens(estimate_tokens(prompt)):
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_STAGE_INPUT_OVER_WINDOW, stage="fragment"))
        replacement_input_tokens = fragment_replacement_input_tokens(fragment)
        generated = await call_fixed_summary_model(
            model=model,
            prompt=prompt,
            input_tokens=replacement_input_tokens,
        )
        return SummaryFragmentResult(
            fragment_index=fragment.fragment_index,
            message_start_id=fragment.message_start_id,
            message_end_id=fragment.message_end_id,
            content=generated,
            token_count=estimate_tokens(generated),
        )

    try:
        stats = await run_bounded_fragment_pipeline(
            fragments=fragments,
            expected_fragment_count=expected_fragment_count,
            process_fragment=process_fragment,
            first_fragment_index=first_fragment_index,
            persist_fragment=lambda result: write_summary_fragment(
                stage=persisted_stage,
                result=result,
            ),
        )
        await ensure_context_summary_work_valid(work_validity_checker)
        if not await mark_summary_stage_completed(stage=persisted_stage):
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_STAGE_COMPLETION_FAILED, stage="fragment"))
    except Exception as exc:
        error = format_context_summary_exception(exc)
        logger.bind(
            uid=uid,
            session_id=session_id,
            stage_key=stage_key,
            model_key=model_key,
            channel_id=model.channel_id,
            channel_name=model.channel_name,
            model_id=model.model_id,
            expected_fragment_count=expected_fragment_count,
            first_fragment_index=first_fragment_index,
            exception_type=type(exc).__name__,
            exception_detail=error,
        ).opt(exception=exc).error("Context summary fragment stage failed")
        await mark_summary_stage_failed(stage=persisted_stage, error=error)
        await invalidate_summary_stage(stage=persisted_stage)
        if contains_context_summary_work_invalid(exc):
            raise ContextSummaryWorkInvalidError(t(ERR_CONTEXT_SUMMARY_WORK_INVALID_DURING, stage="fragment execution")) from exc
        raise ContextSummaryLayerError(t(ERR_CONTEXT_SUMMARY_LAYER_FAILED, error=error)) from exc

    logger.bind(
        uid=uid,
        session_id=session_id,
        stage_key=stage_key,
        model_key=model_key,
        expected_fragment_count=expected_fragment_count,
        max_active_tasks=stats.max_active_tasks,
        max_input_queue_size=stats.max_input_queue_size,
        max_result_queue_size=stats.max_result_queue_size,
        max_reorder_size=stats.max_reorder_size,
    ).debug("Context summary fragment stage passed validation and was completed")

    from app.core.utils.context_summary.reduction import (
        reduce_completed_summary_stage_result,
    )

    reduced = await reduce_completed_summary_stage_result(
        db,
        profile=profile,
        cfg=cfg,
        initial_stage=persisted_stage,
        safety_margin_tokens=safety_margin_tokens,
        work_validity_checker=work_validity_checker,
    )
    return GeneratedSummaryResult(
        content=reduced.content,
        message_count=summarized_message_count,
        completed_stage=reduced.stage,
    )


async def generate_snapshot_summary_result(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    snapshot: ContextSummarySnapshot,
    existing_summary: str | None,
    existing_summary_revision: int,
    safety_margin_tokens: int,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
) -> GeneratedSummaryResult:
    excluded_priorities: set[int] = set()
    call_context = "context_summary"

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
        if model is None or model.priority in excluded_priorities:
            _total_tokens, summarized_message_count = await measure_persistent_history(
                db,
                session_id=session_id,
                uid=uid,
                snapshot=snapshot,
            )
            await release_db_session(db)
            return GeneratedSummaryResult(
                content=None,
                message_count=summarized_message_count,
            )

        try:
            return await generate_snapshot_summary_with_model(
                db,
                session_id=session_id,
                uid=uid,
                profile=profile,
                cfg=cfg,
                snapshot=snapshot,
                existing_summary=existing_summary,
                existing_summary_revision=existing_summary_revision,
                safety_margin_tokens=safety_margin_tokens,
                model=model,
                work_validity_checker=work_validity_checker,
            )
        except ContextSummaryWorkInvalidError:
            raise
        except ContextSummaryLayerError as exc:
            excluded_priorities.add(model.priority)
            call_context = "context_summary_retry"
            logger.bind(
                uid=uid,
                session_id=session_id,
                channel_id=model.channel_id,
                channel_name=model.channel_name,
                model_id=model.model_id,
                stage_priority=model.priority,
            ).warning(
                "Context summary layer invalidated before model fallback: {error}",
                error=str(exc),
            )


async def generate_snapshot_summary(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    snapshot: ContextSummarySnapshot,
    existing_summary: str | None,
    existing_summary_revision: int,
    safety_margin_tokens: int,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
) -> tuple[str | None, int]:
    result = await generate_snapshot_summary_result(
        db,
        session_id=session_id,
        uid=uid,
        profile=profile,
        cfg=cfg,
        snapshot=snapshot,
        existing_summary=existing_summary,
        existing_summary_revision=existing_summary_revision,
        safety_margin_tokens=safety_margin_tokens,
        work_validity_checker=work_validity_checker,
    )
    return result.content, result.message_count
