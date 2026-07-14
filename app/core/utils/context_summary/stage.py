import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.context_summary_fragment import (
    build_context_summary_fragment_dedupe_key,
    context_summary_fragment_crud,
)
from app.core.crud.context_summary_stage import context_summary_stage_crud
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
from app.core.utils.dispatcher.helpers import format_exception_message
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
    for _attempt in range(CONTEXT_SUMMARY_MODEL_ATTEMPTS):
        try:
            generated = await call_context_summary_model(model=model, prompt=prompt)
            if not generated:
                raise RuntimeError("Context summary model returned an empty result")
            if estimate_tokens(generated) >= input_tokens:
                raise RuntimeError("Context summary model did not reduce its input")
            return generated
        except Exception as exc:
            last_error = exc

    raise RuntimeError("Context summary model failed after fixed-model retries") from last_error


def fragment_prompt(fragment: SummaryFragmentInput) -> str:
    return CONTEXT_SUMMARY_PROMPT.format(
        existing_summary=fragment.existing_summary or "(none)",
        recent_dialogue="(none)",
        conversation=fragment.content,
    )


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
        raise RuntimeError("Context summary fragment could not be written in order")
    if not created and (persisted.message_start_id != result.message_start_id or persisted.message_end_id != result.message_end_id or persisted.content != result.content):
        raise RuntimeError("Context summary fragment conflicts with an existing result")


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
        raise ContextSummaryLayerError("Context summary model has no usable input budget")

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
        raise ContextSummaryLayerError("Context summary layer cannot be split for the selected model")

    snapshot_max_message_id = snapshot.snapshot_max_message_id
    persistent_summary_target_id = snapshot.persistent_summary_target_id
    if snapshot_max_message_id is None or persistent_summary_target_id is None:
        raise ContextSummaryLayerError("Context summary snapshot has no persistent range")

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
        context_window_k=max(1, model.context_window_tokens // 1024),
        max_output_tokens=model.max_output_tokens,
        safety_margin_tokens=model.safety_margin_tokens,
        expected_summary_message_id=snapshot.expected_summary_message_id,
        expected_summary_revision=existing_summary_revision,
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
        raise ContextSummaryLayerError("Context summary layer conflicts with an existing stage")

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
        error = "Context summary fragment stage failed completion validation"
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
            raise RuntimeError("Context summary fragment exceeds the selected model window")
        generated = await call_fixed_summary_model(
            model=model,
            prompt=prompt,
            input_tokens=fragment.token_count,
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
            raise RuntimeError("Context summary fragment stage failed completion validation")
    except Exception as exc:
        error = format_exception_message(exc)
        await mark_summary_stage_failed(stage=persisted_stage, error=error)
        await invalidate_summary_stage(stage=persisted_stage)
        if contains_context_summary_work_invalid(exc):
            raise ContextSummaryWorkInvalidError("Context summary work became invalid during fragment execution") from exc
        raise ContextSummaryLayerError("Context summary fragment layer failed") from exc

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
                error=format_exception_message(exc),
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
