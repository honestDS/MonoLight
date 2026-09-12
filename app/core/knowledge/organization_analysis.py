from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    KNOWLEDGE_ORGANIZATION_MODEL_MAX_ATTEMPTS,
    KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS,
)
from app.core.crud.knowledge.organization import knowledge_organization_fragment_crud
from app.core.i18n import t
from app.core.knowledge import organization_pipeline, organization_scope, organization_stages
from app.core.knowledge.errors import (
    KnowledgeOrganizationContextExceededError,
    KnowledgeOrganizationExecutionError,
    KnowledgeOrganizationModelFailedError,
    KnowledgeOrganizationNotConvergedError,
)
from app.core.knowledge.organization_runtime import (
    KnowledgeOrganizationModelConfig,
    split_content_for_analysis,
)
from app.core.knowledge.organization_types import (
    KnowledgeOrganizationAnalysisResult,
    KnowledgeOrganizationScopeItem,
)
from app.core.prompts import KNOWLEDGE_ORGANIZATION_ANALYSIS_SYSTEM_PROMPT
from app.core.utils.tokenizer import estimate_tokens
from app.models.knowledge_base import (
    KnowledgeOrganizationFragment,
    KnowledgeOrganizationFragmentStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationStage,
    KnowledgeOrganizationStageStatus,
)
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient

__all__ = []


async def _default_analysis_caller(model: KnowledgeOrganizationModelConfig, *, content: str) -> str:
    payload = canonical_json_dumps({"content": content})
    prompt_tokens = estimate_tokens(KNOWLEDGE_ORGANIZATION_ANALYSIS_SYSTEM_PROMPT)
    analysis_max_output_tokens = min(model.max_output_tokens, KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS)
    available = model.context_window_tokens - analysis_max_output_tokens - model.safety_margin_tokens - prompt_tokens
    if estimate_tokens(payload) > available:
        raise KnowledgeOrganizationContextExceededError()
    response = await LLMClient.generate(
        api_key=model.api_key,
        base_url=model.base_url,
        model_id=model.model_id,
        messages=[
            InternalMessage(role=MessageRole.SYSTEM, content=KNOWLEDGE_ORGANIZATION_ANALYSIS_SYSTEM_PROMPT),
            InternalMessage(role=MessageRole.USER, content=payload),
        ],
        temperature=model.temperature,
        top_p=model.top_p,
        max_tokens=analysis_max_output_tokens,
        tools=None,
        protocol=model.protocol,
        timeout=model.timeout,
        request_context_tokens=prompt_tokens + estimate_tokens(payload),
        http_proxy=model.http_proxy,
        custom_headers=dict(model.custom_headers),
    )
    content_value = response.message.content
    if not isinstance(content_value, str):
        raise ValueError(t("organization analysis result is not text"))
    try:
        parsed = KnowledgeOrganizationAnalysisResult.model_validate(json.loads(content_value))
    except Exception as exc:
        raise ValueError(t("organization analysis result invalid")) from exc
    if estimate_tokens(parsed.summary) > KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS:
        raise ValueError(t("organization analysis summary too long"))
    return parsed.summary


def _analysis_payload_tokens(content: str) -> int:
    return max(1, estimate_tokens(canonical_json_dumps({"content": content})))


def _split_analysis_content_for_payload(content: str, *, max_payload_tokens: int) -> tuple[str, ...]:
    if not isinstance(content, str) or not content or max_payload_tokens < 1:
        raise KnowledgeOrganizationContextExceededError()

    max_content_tokens = max(1, min(estimate_tokens(content), max_payload_tokens))
    while True:
        parts = split_content_for_analysis(content, max_tokens=max_content_tokens)
        payload_sizes = tuple(_analysis_payload_tokens(part) for part in parts)
        if all(size <= max_payload_tokens for size in payload_sizes):
            return parts
        if max_content_tokens == 1:
            raise KnowledgeOrganizationContextExceededError()
        largest_payload = max(payload_sizes)
        next_limit = max(1, (max_content_tokens * max_payload_tokens) // largest_payload)
        if next_limit >= max_content_tokens:
            next_limit = max_content_tokens - 1
        max_content_tokens = next_limit


async def _read_analysis_stage_text(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> str:
    summaries: list[str] = []
    async for fragment in organization_stages._iter_stage_fragments(session_factory, stage=stage):
        summary = fragment.result.get("summary") if isinstance(fragment.result, dict) else None
        if not isinstance(summary, str) or not summary:
            raise RuntimeError(t("organization analysis fragment invalid"))
        summaries.append(summary)
    return "\n".join(summaries)


async def _execute_analysis_stage(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    work_key: str,
    model: KnowledgeOrganizationModelConfig,
    scope_item: KnowledgeOrganizationScopeItem,
    content: str,
    analysis_layer: int,
    analysis_caller: Callable[..., Any],
) -> tuple[str, KnowledgeOrganizationStage]:
    analysis_prompt_tokens = estimate_tokens(KNOWLEDGE_ORGANIZATION_ANALYSIS_SYSTEM_PROMPT)
    analysis_max_output_tokens = min(model.max_output_tokens, KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS)
    max_payload_tokens = model.context_window_tokens - analysis_max_output_tokens - model.safety_margin_tokens - analysis_prompt_tokens
    if max_payload_tokens <= 0:
        raise KnowledgeOrganizationContextExceededError()
    parts = _split_analysis_content_for_payload(content, max_payload_tokens=max_payload_tokens)
    expected_count = len(parts)
    analysis_input_key = hashlib.sha256(
        canonical_json_dumps(
            {
                "sources": [[knowledge_id, version] for knowledge_id, version in scope_item.sources],
                "knowledge_key": scope_item.knowledge_key,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
        ).encode("utf-8")
    ).hexdigest()
    purpose = f"analysis:{analysis_input_key}:{analysis_layer}"
    stage = await organization_stages._create_stage(
        session_factory,
        snapshot=snapshot,
        work_key=work_key,
        stage_index=analysis_layer,
        lower_stage_key=None,
        model=model,
        expected_fragment_count=expected_count,
        purpose=purpose,
    )
    if stage.status == KnowledgeOrganizationStageStatus.INVALIDATED:
        raise KnowledgeOrganizationExecutionError()
    if stage.status == KnowledgeOrganizationStageStatus.COMPLETED:
        reduced = await _read_analysis_stage_text(session_factory, stage=stage)
        if estimate_tokens(reduced) >= estimate_tokens(content):
            exc = KnowledgeOrganizationNotConvergedError()
            await organization_stages._fail_and_invalidate_stage(session_factory, stage=stage, error=f"{type(exc).__name__}: {exc}")
            raise exc
        return reduced, stage

    last_error: BaseException | None = None
    for attempt in range(KNOWLEDGE_ORGANIZATION_MODEL_MAX_ATTEMPTS):
        first_index = await organization_stages._stage_resume_index(session_factory, stage=stage)

        async def inputs() -> AsyncIterator[tuple[int, str]]:
            for index in range(first_index, expected_count):
                yield index, parts[index]

        async def process(value: tuple[int, str]) -> tuple[int, str, str]:
            index, part = value
            summary = await organization_pipeline.maybe_await(analysis_caller(model, content=part))
            if not isinstance(summary, str) or not summary.strip() or estimate_tokens(summary) > KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS:
                raise ValueError(t("organization analysis summary invalid"))
            return index, part, summary

        async def persist(value: tuple[int, str, str]) -> None:
            index, part, summary = value
            fragment = KnowledgeOrganizationFragment(
                dedupe_key=knowledge_organization_fragment_crud.build_dedupe_key(
                    work_key=stage.work_key,
                    stage_key=stage.stage_key,
                    model_key=stage.model_key,
                    fragment_index=index,
                ),
                uid=stage.uid,
                knowledge_base_id=stage.knowledge_base_id,
                snapshot_id=stage.snapshot_id,
                stage_id=stage.id,
                work_key=stage.work_key,
                snapshot_key=stage.snapshot_key,
                stage_key=stage.stage_key,
                model_key=stage.model_key,
                fragment_index=index,
                candidate_scope={
                    "sources": [[knowledge_id, version] for knowledge_id, version in scope_item.sources],
                    "part_index": index,
                    "content_hash": hashlib.sha256(part.encode("utf-8")).hexdigest(),
                },
                result={"summary": summary},
                status=KnowledgeOrganizationFragmentStatus.COMPLETED,
            )
            async with session_factory() as db:
                persisted, _created = await knowledge_organization_fragment_crud.write_ordered(db, fragment=fragment)
            if persisted is None or persisted.candidate_scope != fragment.candidate_scope or persisted.result != fragment.result:
                raise RuntimeError(t("organization analysis fragment persistence conflict"))

        try:
            if first_index < expected_count:
                await organization_pipeline.run_bounded_knowledge_organization_pipeline(
                    inputs=inputs(),
                    expected_count=expected_count,
                    process=process,
                    persist=persist,
                    first_index=first_index,
                )
            reduced = await _read_analysis_stage_text(session_factory, stage=stage)
            if estimate_tokens(reduced) >= estimate_tokens(content):
                raise KnowledgeOrganizationNotConvergedError()
            await organization_stages._mark_stage_completed(session_factory, stage=stage)
            return reduced, stage
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


async def _compact_scope_item(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    work_key: str,
    model: KnowledgeOrganizationModelConfig,
    item: KnowledgeOrganizationScopeItem,
    analysis_caller: Callable[..., Any],
) -> tuple[KnowledgeOrganizationScopeItem, int]:
    if organization_scope._scope_tokens((item,)) <= model.input_budget_tokens:
        return item, 0
    minimum_item = KnowledgeOrganizationScopeItem(
        sources=item.sources,
        knowledge_key=item.knowledge_key,
        content="x",
        content_hash=item.content_hash,
        source_type=item.source_type,
        source_reference=item.source_reference,
        vector_item_ids=item.vector_item_ids,
        related_ids=item.related_ids,
        effective_item=item.effective_item,
    )
    if organization_scope._scope_tokens((minimum_item,)) > model.input_budget_tokens:
        raise KnowledgeOrganizationContextExceededError()
    content = item.content
    stage_count = 0
    analysis_layer = 0
    while True:
        reduced, _stage = await _execute_analysis_stage(
            session_factory,
            snapshot=snapshot,
            work_key=work_key,
            model=model,
            scope_item=item,
            content=content,
            analysis_layer=analysis_layer,
            analysis_caller=analysis_caller,
        )
        stage_count += 1
        content = reduced
        compacted = KnowledgeOrganizationScopeItem(
            sources=item.sources,
            knowledge_key=item.knowledge_key,
            content=content,
            content_hash=item.content_hash,
            source_type=item.source_type,
            source_reference=item.source_reference,
            vector_item_ids=item.vector_item_ids,
            related_ids=item.related_ids,
            effective_item=item.effective_item,
        )
        if organization_scope._scope_tokens((compacted,)) <= model.input_budget_tokens:
            return compacted, stage_count
        analysis_layer += 1


async def _prepare_scope_for_model(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    work_key: str,
    model: KnowledgeOrganizationModelConfig,
    scope: tuple[KnowledgeOrganizationScopeItem, ...],
    analysis_caller: Callable[..., Any],
) -> tuple[tuple[KnowledgeOrganizationScopeItem, ...], int]:
    prepared: list[KnowledgeOrganizationScopeItem] = []
    analysis_stage_count = 0
    for item in scope:
        compacted, added_stages = await _compact_scope_item(
            session_factory,
            snapshot=snapshot,
            work_key=work_key,
            model=model,
            item=item,
            analysis_caller=analysis_caller,
        )
        prepared.append(compacted)
        analysis_stage_count += added_stages
    result = tuple(prepared)
    if organization_scope._scope_tokens(result) > model.input_budget_tokens:
        raise KnowledgeOrganizationContextExceededError()
    return result, analysis_stage_count
