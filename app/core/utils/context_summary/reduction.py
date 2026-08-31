import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_CONTEXT_SUMMARY_FINAL_RESULT_EMPTY,
    ERR_CONTEXT_SUMMARY_FINAL_STAGE_EMPTY,
    ERR_CONTEXT_SUMMARY_FINAL_STAGE_MULTIPLE,
    ERR_CONTEXT_SUMMARY_MODEL_NO_INPUT_BUDGET,
    ERR_CONTEXT_SUMMARY_MODELS_EXHAUSTED,
    ERR_CONTEXT_SUMMARY_REDUCTION_NO_INPUT_GROUPS,
    ERR_CONTEXT_SUMMARY_REDUCTION_STAGE_CONFLICT,
    ERR_CONTEXT_SUMMARY_REFINEMENT_LOWER_INVALID,
    ERR_CONTEXT_SUMMARY_STAGE_COMPLETION_FAILED,
    ERR_CONTEXT_SUMMARY_STAGE_INPUT_OVER_WINDOW,
    ERR_CONTEXT_SUMMARY_STAGE_NOT_REDUCED,
    ERR_CONTEXT_SUMMARY_WORK_INVALID_DURING,
)
from app.core.crud.context_summary.stage import context_summary_stage_crud
from app.core.i18n import t
from app.core.prompts import CONTEXT_SUMMARY_COMPRESS_PROMPT, CONTEXT_SUMMARY_PROMPT
from app.core.utils.context_summary.common import (
    ContextSummaryWorkInvalidError,
    ContextSummaryWorkValidityChecker,
    contains_context_summary_work_invalid,
    ensure_context_summary_work_valid,
)
from app.core.utils.context_summary.merge import (
    count_lower_stage_merge_groups,
    iter_completed_lower_stage_fragments,
    iter_lower_stage_merge_groups,
)
from app.core.utils.context_summary.pipeline import (
    SummaryFragmentInput,
    SummaryFragmentResult,
    run_bounded_fragment_pipeline,
)
from app.core.utils.context_summary.selection import (
    ContextSummaryModelSnapshot,
    select_context_summary_model,
)
from app.core.utils.dispatcher.helpers import format_exception_message
from app.core.utils.tokenizer import estimate_tokens
from app.models.context_summary_stage import (
    ContextSummaryFragment,
    ContextSummaryStage,
    ContextSummaryStageStatus,
)
from app.models.profile import Profile, ProfileConfig
from app.providers.database import AsyncSessionLocal


@dataclass(frozen=True)
class CompletedSummaryResult:
    content: str
    stage: ContextSummaryStage


def merge_fragment_prompt(fragment: SummaryFragmentInput) -> str:
    return CONTEXT_SUMMARY_PROMPT.format(
        existing_summary="(none)",
        recent_dialogue="(none)",
        conversation=fragment.content,
    )


def build_reduction_stage_identity(
    *,
    lower_stage: ContextSummaryStage,
    model: ContextSummaryModelSnapshot,
    expected_fragment_count: int,
    refinement_index: int,
) -> tuple[str, str]:
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
    stage_payload = json.dumps(
        {
            "snapshot_key": lower_stage.snapshot_key,
            "lower_stage_key": lower_stage.stage_key,
            "model_key": model_key,
            "expected_fragment_count": expected_fragment_count,
            "refinement_index": refinement_index,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    stage_key = hashlib.sha256(stage_payload.encode("utf-8")).hexdigest()
    return stage_key, model_key


async def measure_completed_stage_tokens(stage: ContextSummaryStage) -> int:
    total_tokens = 0
    async for fragment in iter_completed_lower_stage_fragments(
        work_dedupe_key=stage.work_dedupe_key,
        lower_stage_key=stage.stage_key,
    ):
        total_tokens += max(1, estimate_tokens(fragment.content))
    return total_tokens


async def measure_running_stage_tokens(stage: ContextSummaryStage) -> int:
    async with AsyncSessionLocal() as fragment_db:
        result = await fragment_db.execute(
            select(func.coalesce(func.sum(ContextSummaryFragment.token_count), 0)).where(
                ContextSummaryFragment.work_dedupe_key == stage.work_dedupe_key,
                ContextSummaryFragment.stage_key == stage.stage_key,
                ContextSummaryFragment.model_key == stage.model_key,
            )
        )
        return int(result.scalar_one())


async def read_single_completed_summary(stage: ContextSummaryStage) -> str:
    fragments = iter_completed_lower_stage_fragments(
        work_dedupe_key=stage.work_dedupe_key,
        lower_stage_key=stage.stage_key,
    )
    fragment = await anext(fragments, None)
    if fragment is None:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FINAL_STAGE_EMPTY))
    if await anext(fragments, None) is not None:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FINAL_STAGE_MULTIPLE))
    if not fragment.content:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_FINAL_RESULT_EMPTY))
    return fragment.content


async def create_reduction_stage(
    *,
    lower_stage: ContextSummaryStage,
    model: ContextSummaryModelSnapshot,
    expected_fragment_count: int,
    refinement_index: int,
) -> ContextSummaryStage:
    stage_key, model_key = build_reduction_stage_identity(
        lower_stage=lower_stage,
        model=model,
        expected_fragment_count=expected_fragment_count,
        refinement_index=refinement_index,
    )
    stage = ContextSummaryStage(
        uid=lower_stage.uid,
        session_id=lower_stage.session_id,
        work_id=lower_stage.work_id,
        work_dedupe_key=lower_stage.work_dedupe_key,
        snapshot_key=lower_stage.snapshot_key,
        stage_key=stage_key,
        lower_stage_key=lower_stage.stage_key,
        model_key=model_key,
        channel_id=model.channel_id,
        model_id=model.model_id,
        context_window_k=max(1, model.context_window_tokens // CONTEXT_WINDOW_TOKENS_PER_K),
        max_output_tokens=model.max_output_tokens,
        safety_margin_tokens=model.safety_margin_tokens,
        expected_summary_message_id=lower_stage.expected_summary_message_id,
        expected_summary_revision=lower_stage.expected_summary_revision,
        expected_content_revision=lower_stage.expected_content_revision,
        snapshot_max_message_id=lower_stage.snapshot_max_message_id,
        persistent_summary_target_id=lower_stage.persistent_summary_target_id,
        expected_fragment_count=expected_fragment_count,
    )
    async with AsyncSessionLocal() as stage_db:
        persisted, _created = await context_summary_stage_crud.create_stage(
            stage_db,
            stage=stage,
        )
    if (
        persisted.lower_stage_key != lower_stage.stage_key
        or persisted.model_key != model_key
        or persisted.expected_fragment_count != expected_fragment_count
        or persisted.status
        not in {
            ContextSummaryStageStatus.RUNNING,
            ContextSummaryStageStatus.COMPLETED,
        }
        or persisted.succeeded_fragment_count > expected_fragment_count
        or (persisted.status == ContextSummaryStageStatus.COMPLETED and persisted.succeeded_fragment_count != expected_fragment_count)
    ):
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_REDUCTION_STAGE_CONFLICT))
    return persisted


async def execute_reduction_stage(
    *,
    lower_stage: ContextSummaryStage,
    model: ContextSummaryModelSnapshot,
    refinement_index: int,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
) -> ContextSummaryStage:
    from app.core.utils.context_summary.stage import (
        call_fixed_summary_model,
        invalidate_summary_stage,
        mark_summary_stage_completed,
        mark_summary_stage_failed,
        write_summary_fragment,
    )

    await ensure_context_summary_work_valid(work_validity_checker)
    empty_prompt = merge_fragment_prompt(
        SummaryFragmentInput(
            fragment_index=0,
            message_start_id=lower_stage.expected_summary_message_id or 1,
            message_end_id=lower_stage.persistent_summary_target_id,
            token_count=1,
            content="(none)",
        )
    )
    max_group_tokens = model.input_budget_tokens - estimate_tokens(empty_prompt) - 32
    if max_group_tokens <= 0:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_MODEL_NO_INPUT_BUDGET, stage="reduction"))

    expected_fragment_count = await count_lower_stage_merge_groups(
        work_dedupe_key=lower_stage.work_dedupe_key,
        lower_stage_key=lower_stage.stage_key,
        max_group_tokens=max_group_tokens,
    )
    if expected_fragment_count <= 0:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_REDUCTION_NO_INPUT_GROUPS))

    await ensure_context_summary_work_valid(work_validity_checker)
    stage = await create_reduction_stage(
        lower_stage=lower_stage,
        model=model,
        expected_fragment_count=expected_fragment_count,
        refinement_index=refinement_index,
    )
    first_fragment_index = stage.succeeded_fragment_count
    if stage.status == ContextSummaryStageStatus.COMPLETED:
        return stage

    groups = iter_lower_stage_merge_groups(
        work_dedupe_key=lower_stage.work_dedupe_key,
        lower_stage_key=lower_stage.stage_key,
        max_group_tokens=max_group_tokens,
        first_group_index=first_fragment_index,
    )

    async def process_group(group: SummaryFragmentInput) -> SummaryFragmentResult:
        await ensure_context_summary_work_valid(work_validity_checker)
        prompt = merge_fragment_prompt(group)
        if not model.accepts_prompt_tokens(estimate_tokens(prompt)):
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_STAGE_INPUT_OVER_WINDOW, stage="reduction group"))
        generated = await call_fixed_summary_model(
            model=model,
            prompt=prompt,
            input_tokens=group.token_count,
        )
        return SummaryFragmentResult(
            fragment_index=group.fragment_index,
            message_start_id=group.message_start_id,
            message_end_id=group.message_end_id,
            content=generated,
            token_count=estimate_tokens(generated),
        )

    try:
        if first_fragment_index < expected_fragment_count:
            await run_bounded_fragment_pipeline(
                fragments=groups,
                expected_fragment_count=expected_fragment_count,
                process_fragment=process_group,
                first_fragment_index=first_fragment_index,
                persist_fragment=lambda result: write_summary_fragment(
                    stage=stage,
                    result=result,
                ),
            )
        await ensure_context_summary_work_valid(work_validity_checker)
        lower_tokens = await measure_completed_stage_tokens(lower_stage)
        output_tokens = await measure_running_stage_tokens(stage)
        if output_tokens >= lower_tokens:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_STAGE_NOT_REDUCED, stage="reduction"))
        if not await mark_summary_stage_completed(stage=stage):
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_STAGE_COMPLETION_FAILED, stage="reduction"))
    except Exception as exc:
        await mark_summary_stage_failed(
            stage=stage,
            error=format_exception_message(exc),
        )
        await invalidate_summary_stage(stage=stage)
        if contains_context_summary_work_invalid(exc):
            raise ContextSummaryWorkInvalidError(t(ERR_CONTEXT_SUMMARY_WORK_INVALID_DURING, stage="reduction")) from exc
        raise

    return stage


async def reduce_completed_summary_stage_result(
    db: AsyncSession,
    *,
    profile: Profile,
    cfg: ProfileConfig,
    initial_stage: ContextSummaryStage,
    safety_margin_tokens: int,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
) -> CompletedSummaryResult:
    lower_stage = initial_stage
    refinement_index = 0

    while True:
        await ensure_context_summary_work_valid(work_validity_checker)
        if lower_stage.expected_fragment_count == 1:
            return CompletedSummaryResult(
                content=await read_single_completed_summary(lower_stage),
                stage=lower_stage,
            )

        excluded_priorities: set[int] = set()
        while True:
            await ensure_context_summary_work_valid(work_validity_checker)
            model = await select_context_summary_model(
                db,
                profile_id=profile.id,
                channel_config=cfg.channel.context_summary_channel,
                safety_margin_tokens=safety_margin_tokens,
                excluded_priorities=set(excluded_priorities),
                call_context=("context_summary" if not excluded_priorities else "context_summary_retry"),
            )
            if model is None or model.priority in excluded_priorities:
                raise RuntimeError(t(ERR_CONTEXT_SUMMARY_MODELS_EXHAUSTED, stage="reduction"))
            try:
                next_stage = await execute_reduction_stage(
                    lower_stage=lower_stage,
                    model=model,
                    refinement_index=refinement_index,
                    work_validity_checker=work_validity_checker,
                )
                break
            except ContextSummaryWorkInvalidError:
                raise
            except Exception:
                excluded_priorities.add(model.priority)

        lower_stage = next_stage
        refinement_index += 1


async def execute_refinement_stage(
    *,
    lower_stage: ContextSummaryStage,
    model: ContextSummaryModelSnapshot,
    refinement_index: int,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
) -> ContextSummaryStage:
    from app.core.utils.context_summary.stage import (
        call_fixed_summary_model,
        invalidate_summary_stage,
        mark_summary_stage_completed,
        mark_summary_stage_failed,
        write_summary_fragment,
    )

    await ensure_context_summary_work_valid(work_validity_checker)
    lower_fragments = iter_completed_lower_stage_fragments(
        work_dedupe_key=lower_stage.work_dedupe_key,
        lower_stage_key=lower_stage.stage_key,
    )
    lower_fragment = await anext(lower_fragments, None)
    if lower_fragment is None or await anext(lower_fragments, None) is not None:
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_REFINEMENT_LOWER_INVALID))

    prompt = CONTEXT_SUMMARY_COMPRESS_PROMPT.format(
        summary=lower_fragment.content,
    )
    prompt_tokens = estimate_tokens(prompt)
    if not model.accepts_prompt_tokens(prompt_tokens):
        raise RuntimeError(t(ERR_CONTEXT_SUMMARY_STAGE_INPUT_OVER_WINDOW, stage="refinement"))

    await ensure_context_summary_work_valid(work_validity_checker)
    stage = await create_reduction_stage(
        lower_stage=lower_stage,
        model=model,
        expected_fragment_count=1,
        refinement_index=refinement_index,
    )
    if stage.status == ContextSummaryStageStatus.COMPLETED:
        return stage

    try:
        if stage.succeeded_fragment_count == 0:
            await ensure_context_summary_work_valid(work_validity_checker)
            generated = await call_fixed_summary_model(
                model=model,
                prompt=prompt,
                input_tokens=max(1, estimate_tokens(lower_fragment.content)),
            )
            await write_summary_fragment(
                stage=stage,
                result=SummaryFragmentResult(
                    fragment_index=0,
                    message_start_id=lower_fragment.message_start_id,
                    message_end_id=lower_fragment.message_end_id,
                    content=generated,
                    token_count=max(1, estimate_tokens(generated)),
                ),
            )
        await ensure_context_summary_work_valid(work_validity_checker)
        output_tokens = await measure_running_stage_tokens(stage)
        lower_tokens = max(1, estimate_tokens(lower_fragment.content))
        if output_tokens >= lower_tokens:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_STAGE_NOT_REDUCED, stage="refinement"))
        if not await mark_summary_stage_completed(stage=stage):
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_STAGE_COMPLETION_FAILED, stage="refinement"))
    except Exception as exc:
        await mark_summary_stage_failed(
            stage=stage,
            error=format_exception_message(exc),
        )
        await invalidate_summary_stage(stage=stage)
        if contains_context_summary_work_invalid(exc):
            raise ContextSummaryWorkInvalidError(t(ERR_CONTEXT_SUMMARY_WORK_INVALID_DURING, stage="refinement")) from exc
        raise

    return stage


async def refine_completed_summary_stage(
    db: AsyncSession,
    *,
    profile: Profile,
    cfg: ProfileConfig,
    lower_stage: ContextSummaryStage,
    safety_margin_tokens: int,
    refinement_index: int,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
) -> CompletedSummaryResult:
    excluded_priorities: set[int] = set()
    while True:
        await ensure_context_summary_work_valid(work_validity_checker)
        model = await select_context_summary_model(
            db,
            profile_id=profile.id,
            channel_config=cfg.channel.context_summary_channel,
            safety_margin_tokens=safety_margin_tokens,
            excluded_priorities=set(excluded_priorities),
            call_context=("context_summary" if not excluded_priorities else "context_summary_retry"),
        )
        if model is None or model.priority in excluded_priorities:
            raise RuntimeError(t(ERR_CONTEXT_SUMMARY_MODELS_EXHAUSTED, stage="refinement"))
        try:
            stage = await execute_refinement_stage(
                lower_stage=lower_stage,
                model=model,
                refinement_index=refinement_index,
                work_validity_checker=work_validity_checker,
            )
            return CompletedSummaryResult(
                content=await read_single_completed_summary(stage),
                stage=stage,
            )
        except ContextSummaryWorkInvalidError:
            raise
        except Exception:
            excluded_priorities.add(model.priority)


async def reduce_completed_summary_stage(
    db: AsyncSession,
    *,
    profile: Profile,
    cfg: ProfileConfig,
    initial_stage: ContextSummaryStage,
    safety_margin_tokens: int,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
) -> str:
    result = await reduce_completed_summary_stage_result(
        db,
        profile=profile,
        cfg=cfg,
        initial_stage=initial_stage,
        safety_margin_tokens=safety_margin_tokens,
        work_validity_checker=work_validity_checker,
    )
    return result.content
