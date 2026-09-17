from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, TextIO

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    KNOWLEDGE_ORGANIZATION_MODEL_MAX_ATTEMPTS,
    LOG_KNOWLEDGE_ORGANIZATION_FRAGMENT_COMPLETED,
    LOG_KNOWLEDGE_ORGANIZATION_STAGE_SPLIT,
)
from app.core.i18n import t
from app.core.knowledge import (
    organization_analysis,
    organization_pipeline,
    organization_reduction,
    organization_scope,
    organization_stages,
)
from app.core.knowledge.errors import (
    KnowledgeOrganizationContextExceededError,
    KnowledgeOrganizationExecutionError,
    KnowledgeOrganizationModelFailedError,
    KnowledgeOrganizationNotConvergedError,
)
from app.core.knowledge.organization_runtime import KnowledgeOrganizationModelConfig
from app.core.knowledge.organization_types import (
    KnowledgeOrganizationFragmentInput,
    KnowledgeOrganizationFragmentResult,
    KnowledgeOrganizationPipelineStats,
    KnowledgeOrganizationPlan,
    KnowledgeOrganizationScopeItem,
)
from app.core.log import get_logger
from app.core.prompts import KNOWLEDGE_ORGANIZATION_SYSTEM_PROMPT
from app.core.utils.tokenizer import estimate_tokens
from app.models.knowledge_base import (
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationStage,
    KnowledgeOrganizationStageStatus,
)
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient

__all__ = []

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _StageExecutionResult:
    stage: KnowledgeOrganizationStage
    input_tokens: int
    output_tokens: int
    stats: KnowledgeOrganizationPipelineStats | None = None


def _fragment_audit_result(plan: KnowledgeOrganizationPlan) -> str:
    items: list[dict[str, Any]] = []
    for item in plan.items:
        raw = item.model_dump(mode="json")
        target = raw.pop("target", None)
        if isinstance(target, dict):
            raw["target_knowledge_key"] = target.get("knowledge_key")
        items.append(raw)
    return canonical_json_dumps(items)


def _log_stage_split(
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    organization_job_id: int | None,
    stage_index: int,
    purpose: str,
    expected_count: int,
    model: KnowledgeOrganizationModelConfig,
) -> None:
    if organization_job_id is None:
        return
    logger.bind(
        uid=snapshot.uid,
        knowledge_base_id=snapshot.knowledge_base_id,
        job_id=organization_job_id,
        stage_index=stage_index,
        fragment_count=expected_count,
        organization_stage_purpose=purpose,
        model_id=model.model_id,
    ).info(
        t(
            LOG_KNOWLEDGE_ORGANIZATION_STAGE_SPLIT,
            knowledge_base_id=snapshot.knowledge_base_id,
            job_id=organization_job_id,
            stage_index=stage_index,
            purpose=purpose,
            fragment_count=expected_count,
            model_id=model.model_id,
        )
    )


def _log_fragment_result(
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    organization_job_id: int | None,
    stage_index: int,
    expected_count: int,
    result: KnowledgeOrganizationFragmentResult,
) -> None:
    if organization_job_id is None:
        return
    audit_result = _fragment_audit_result(result.plan)
    logger.bind(
        uid=snapshot.uid,
        knowledge_base_id=snapshot.knowledge_base_id,
        job_id=organization_job_id,
        stage_index=stage_index,
        fragment_index=result.fragment_index,
        fragment_count=expected_count,
    ).info(
        t(
            LOG_KNOWLEDGE_ORGANIZATION_FRAGMENT_COMPLETED,
            knowledge_base_id=snapshot.knowledge_base_id,
            job_id=organization_job_id,
            stage_index=stage_index,
            fragment_number=result.fragment_index + 1,
            fragment_count=expected_count,
            result=audit_result,
        )
    )


async def _default_model_caller(
    model: KnowledgeOrganizationModelConfig,
    *,
    scope: tuple[KnowledgeOrganizationScopeItem, ...],
) -> KnowledgeOrganizationPlan:
    payload = canonical_json_dumps([organization_scope._scope_payload(item) for item in scope])
    if estimate_tokens(payload) > model.input_budget_tokens:
        raise KnowledgeOrganizationContextExceededError()
    response = await LLMClient.generate(
        api_key=model.api_key,
        base_url=model.base_url,
        model_id=model.model_id,
        messages=[
            InternalMessage(role=MessageRole.SYSTEM, content=KNOWLEDGE_ORGANIZATION_SYSTEM_PROMPT),
            InternalMessage(role=MessageRole.USER, content=payload),
        ],
        temperature=model.temperature,
        top_p=model.top_p,
        reasoning_effort=model.reasoning_effort,
        max_tokens=model.max_output_tokens,
        tools=None,
        protocol=model.protocol,
        timeout=model.timeout,
        request_context_tokens=estimate_tokens(KNOWLEDGE_ORGANIZATION_SYSTEM_PROMPT) + estimate_tokens(payload),
        http_proxy=model.http_proxy,
        custom_headers=dict(model.custom_headers),
    )
    content = response.message.content
    if not isinstance(content, str):
        raise ValueError(t("organization model result is not text"))
    try:
        plan = KnowledgeOrganizationPlan.model_validate(json.loads(content))
    except Exception as exc:
        raise ValueError(t("organization model result invalid")) from exc
    return organization_scope._validate_scope_plan(plan, scope=scope)


async def _execute_plan_stage_with_known_groups(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    work_key: str,
    stage_index: int,
    lower_stage: KnowledgeOrganizationStage | None,
    model: KnowledgeOrganizationModelConfig,
    model_caller: Callable[..., Any],
    analysis_caller: Callable[..., Any],
    expected_count: int,
    initial_group_spool: TextIO | None = None,
    single_scope: tuple[KnowledgeOrganizationScopeItem, ...] | None = None,
    organization_job_id: int | None = None,
) -> tuple[_StageExecutionResult, int]:
    if expected_count <= 0:
        raise KnowledgeOrganizationExecutionError()

    purpose = "initial" if lower_stage is None else "reduction"
    stage = await organization_stages._create_stage(
        session_factory,
        snapshot=snapshot,
        work_key=work_key,
        stage_index=stage_index,
        lower_stage_key=lower_stage.stage_key if lower_stage is not None else None,
        model=model,
        expected_fragment_count=expected_count,
        purpose=purpose,
    )
    if stage.status == KnowledgeOrganizationStageStatus.INVALIDATED:
        raise KnowledgeOrganizationExecutionError()
    _log_stage_split(
        snapshot=snapshot,
        organization_job_id=organization_job_id,
        stage_index=stage_index,
        purpose=purpose,
        expected_count=expected_count,
        model=model,
    )
    if stage.status == KnowledgeOrganizationStageStatus.COMPLETED:
        output_tokens = await organization_reduction._measure_stage_output_tokens(session_factory, stage=stage)
        try:
            await organization_reduction._validate_reduction_decrease(
                session_factory,
                lower_stage=lower_stage,
                output_tokens=output_tokens,
            )
        except KnowledgeOrganizationNotConvergedError as exc:
            await organization_stages._fail_and_invalidate_stage(session_factory, stage=stage, error=f"{type(exc).__name__}: {exc}")
            raise
        return _StageExecutionResult(stage=stage, input_tokens=0, output_tokens=output_tokens), 0

    last_error: BaseException | None = None
    analysis_stage_count = 0
    last_stats: KnowledgeOrganizationPipelineStats | None = None
    for attempt in range(KNOWLEDGE_ORGANIZATION_MODEL_MAX_ATTEMPTS):
        first_index = await organization_stages._stage_resume_index(session_factory, stage=stage)

        async def inputs() -> AsyncIterator[KnowledgeOrganizationFragmentInput]:
            if single_scope is not None:
                if first_index == 0:
                    yield KnowledgeOrganizationFragmentInput(fragment_index=0, scope=single_scope, input_tokens=organization_scope._scope_tokens(single_scope))
                return
            if lower_stage is None:
                if initial_group_spool is None:
                    raise RuntimeError(t("organization initial group spool missing"))
                async for item in organization_scope._iter_captured_initial_groups(initial_group_spool, first_index=first_index):
                    yield item
                return
            async for item in organization_reduction._iter_reduction_groups(
                session_factory,
                lower_stage=lower_stage,
                model=model,
                first_index=first_index,
            ):
                yield item

        input_token_total = 0

        async def process(fragment: KnowledgeOrganizationFragmentInput) -> KnowledgeOrganizationFragmentResult:
            nonlocal input_token_total, analysis_stage_count
            prepared_scope, added_analysis_stages = await organization_analysis._prepare_scope_for_model(
                session_factory,
                snapshot=snapshot,
                work_key=work_key,
                model=model,
                scope=fragment.scope,
                analysis_caller=analysis_caller,
            )
            analysis_stage_count += added_analysis_stages
            input_tokens = organization_scope._scope_tokens(prepared_scope)
            input_token_total += input_tokens
            plan = await organization_pipeline.maybe_await(model_caller(model, scope=prepared_scope))
            if not isinstance(plan, KnowledgeOrganizationPlan):
                plan = KnowledgeOrganizationPlan.model_validate(plan)
            plan = organization_scope._validate_scope_plan(plan, scope=prepared_scope)
            plan, output_scope = organization_reduction._compose_scope_plan(plan, scope=prepared_scope)
            return KnowledgeOrganizationFragmentResult(
                fragment_index=fragment.fragment_index,
                scope=prepared_scope,
                plan=plan,
                output_scope=output_scope,
                input_tokens=input_tokens,
                output_tokens=organization_scope._plan_tokens(plan),
            )

        try:
            if first_index < expected_count:

                async def persist(result: KnowledgeOrganizationFragmentResult) -> None:
                    await organization_stages._write_plan_fragment(session_factory, stage=stage, result=result)
                    _log_fragment_result(
                        snapshot=snapshot,
                        organization_job_id=organization_job_id,
                        stage_index=stage_index,
                        expected_count=expected_count,
                        result=result,
                    )

                last_stats = await organization_pipeline.run_bounded_knowledge_organization_pipeline(
                    inputs=inputs(),
                    expected_count=expected_count,
                    process=process,
                    persist=persist,
                    first_index=first_index,
                )
            output_tokens = await organization_reduction._measure_stage_output_tokens(session_factory, stage=stage)
            await organization_reduction._validate_reduction_decrease(
                session_factory,
                lower_stage=lower_stage,
                output_tokens=output_tokens,
            )
            await organization_stages._mark_stage_completed(session_factory, stage=stage)
            return _StageExecutionResult(stage=stage, input_tokens=input_token_total, output_tokens=output_tokens, stats=last_stats), analysis_stage_count
        except KnowledgeOrganizationNotConvergedError as exc:
            await organization_stages._fail_and_invalidate_stage(session_factory, stage=stage, error=f"{type(exc).__name__}: {exc}")
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 >= KNOWLEDGE_ORGANIZATION_MODEL_MAX_ATTEMPTS:
                await organization_stages._fail_and_invalidate_stage(session_factory, stage=stage, error=f"{type(exc).__name__}: {exc}")
                raise
    raise KnowledgeOrganizationModelFailedError(
        cause=str(last_error) if last_error is not None else None,
    )


async def _execute_plan_stage_for_model(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    work_key: str,
    stage_index: int,
    lower_stage: KnowledgeOrganizationStage | None,
    model: KnowledgeOrganizationModelConfig,
    collection_name: str,
    model_caller: Callable[..., Any],
    analysis_caller: Callable[..., Any],
    semantic_neighbor_loader: Callable[..., Any],
    single_scope: tuple[KnowledgeOrganizationScopeItem, ...] | None = None,
    organization_job_id: int | None = None,
) -> tuple[_StageExecutionResult, int]:
    if single_scope is not None:
        return await _execute_plan_stage_with_known_groups(
            session_factory,
            snapshot=snapshot,
            work_key=work_key,
            stage_index=stage_index,
            lower_stage=lower_stage,
            model=model,
            model_caller=model_caller,
            analysis_caller=analysis_caller,
            expected_count=1,
            single_scope=single_scope,
            organization_job_id=organization_job_id,
        )

    if lower_stage is None:
        spool, expected_count = await organization_scope._capture_initial_groups(
            organization_scope._iter_initial_groups(
                session_factory,
                snapshot=snapshot,
                model=model,
                collection_name=collection_name,
                semantic_neighbor_loader=semantic_neighbor_loader,
            )
        )
        try:
            return await _execute_plan_stage_with_known_groups(
                session_factory,
                snapshot=snapshot,
                work_key=work_key,
                stage_index=stage_index,
                lower_stage=None,
                model=model,
                model_caller=model_caller,
                analysis_caller=analysis_caller,
                expected_count=expected_count,
                initial_group_spool=spool,
                organization_job_id=organization_job_id,
            )
        finally:
            spool.close()

    expected_count = await organization_scope._count_groups(organization_reduction._iter_reduction_groups(session_factory, lower_stage=lower_stage, model=model))
    return await _execute_plan_stage_with_known_groups(
        session_factory,
        snapshot=snapshot,
        work_key=work_key,
        stage_index=stage_index,
        lower_stage=lower_stage,
        model=model,
        model_caller=model_caller,
        analysis_caller=analysis_caller,
        expected_count=expected_count,
        organization_job_id=organization_job_id,
    )
