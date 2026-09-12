from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.i18n import t
from app.core.knowledge.errors import KnowledgeOrganizationNotConvergedError
from app.core.knowledge.organization_runtime import KnowledgeOrganizationModelConfig
from app.core.knowledge.organization_scope import (
    _scope_item_tokens,
    _scope_tokens,
    _source_refs,
    _validate_scope_plan,
)
from app.core.knowledge.organization_stages import _iter_stage_fragments
from app.core.knowledge.organization_types import (
    KnowledgeOrganizationConflict,
    KnowledgeOrganizationFragmentInput,
    KnowledgeOrganizationKeep,
    KnowledgeOrganizationMerge,
    KnowledgeOrganizationPlan,
    KnowledgeOrganizationPlanItem,
    KnowledgeOrganizationScopeItem,
    KnowledgeOrganizationUpdate,
)
from app.models.knowledge_base import KnowledgeOrganizationStage

__all__ = []


def _scope_item_from_plan_item(
    plan_item: KnowledgeOrganizationPlanItem,
    *,
    descriptors: Mapping[int, Mapping[str, Any]],
) -> KnowledgeOrganizationScopeItem:
    sources = _source_refs(plan_item)
    source_descriptors = [descriptors[knowledge_id] for knowledge_id, _version in sources if knowledge_id in descriptors]
    if len(source_descriptors) != len(sources):
        raise RuntimeError(t("organization lower-stage source descriptor missing"))
    related_ids: set[int] = set()
    for descriptor in source_descriptors:
        related_ids.update(int(value) for value in descriptor.get("related_ids") or [] if isinstance(value, int) and not isinstance(value, bool))
    related_ids.update(knowledge_id for knowledge_id, _version in sources)

    if isinstance(plan_item, (KnowledgeOrganizationUpdate, KnowledgeOrganizationMerge)):
        knowledge_key = plan_item.target.knowledge_key
        content_hash = hashlib.sha256(plan_item.target.content.encode("utf-8")).hexdigest()
    elif isinstance(plan_item, KnowledgeOrganizationKeep):
        knowledge_key = str(source_descriptors[0]["knowledge_key"])
        content_hash = str(source_descriptors[0]["content_hash"])
    else:
        knowledge_key = "conflict-" + "-".join(str(knowledge_id) for knowledge_id, _version in sources)
        content_hash = hashlib.sha256(plan_item.summary.encode("utf-8")).hexdigest()

    source_references = [descriptor.get("source_reference") for descriptor in source_descriptors]
    source_reference = source_references[0] if source_references and all(reference == source_references[0] for reference in source_references) else None
    return KnowledgeOrganizationScopeItem(
        sources=sources,
        knowledge_key=knowledge_key,
        content=plan_item.summary,
        content_hash=content_hash,
        source_type="organization_fragment",
        source_reference=source_reference if isinstance(source_reference, Mapping) else None,
        related_ids=frozenset(related_ids),
        effective_item=plan_item,
    )


def _build_reduction_output_scope_item(
    comparison_item: KnowledgeOrganizationPlanItem,
    *,
    touched: tuple[KnowledgeOrganizationScopeItem, ...],
    effective_item: KnowledgeOrganizationPlanItem,
) -> KnowledgeOrganizationScopeItem:
    related_ids: set[int] = set()
    for item in touched:
        related_ids.update(item.related_ids)
        related_ids.update(item.source_ids)

    if len(touched) == 1:
        knowledge_key = touched[0].knowledge_key
        content_hash = touched[0].content_hash
    elif isinstance(comparison_item, KnowledgeOrganizationMerge):
        knowledge_key = comparison_item.target.knowledge_key
        content_hash = hashlib.sha256(comparison_item.target.content.encode("utf-8")).hexdigest()
    elif isinstance(comparison_item, KnowledgeOrganizationConflict):
        knowledge_key = "conflict-" + "-".join(str(knowledge_id) for knowledge_id, _version in _source_refs(comparison_item))
        content_hash = hashlib.sha256(comparison_item.summary.encode("utf-8")).hexdigest()
    else:
        raise ValueError(t("organization reduction cannot combine candidates with a single-source action"))

    source_references = [item.source_reference for item in touched]
    source_reference = source_references[0] if source_references and all(reference == source_references[0] for reference in source_references) else None
    return KnowledgeOrganizationScopeItem(
        sources=_source_refs(comparison_item),
        knowledge_key=knowledge_key,
        content=comparison_item.summary,
        content_hash=content_hash,
        source_type="organization_fragment",
        source_reference=source_reference,
        related_ids=frozenset(related_ids),
        effective_item=effective_item,
    )


def _resolve_reduction_effective_item(
    plan_item: KnowledgeOrganizationPlanItem,
    *,
    touched: tuple[KnowledgeOrganizationScopeItem, ...],
) -> KnowledgeOrganizationPlanItem:
    if len(touched) > 1:
        if not isinstance(plan_item, (KnowledgeOrganizationMerge, KnowledgeOrganizationConflict)):
            raise ValueError(t("organization reduction cross-candidate action invalid"))
        if isinstance(plan_item, KnowledgeOrganizationMerge) and any(isinstance(candidate.effective_item, KnowledgeOrganizationConflict) for candidate in touched):
            raise ValueError(t("organization reduction cannot merge an unresolved conflict"))
        return plan_item

    candidate = touched[0]
    if len(candidate.sources) == 1:
        if isinstance(plan_item, KnowledgeOrganizationKeep):
            return candidate.effective_item or plan_item
        if isinstance(plan_item, KnowledgeOrganizationUpdate):
            return plan_item
        raise ValueError(t("organization reduction single candidate cannot create merge or conflict"))

    existing = candidate.effective_item
    if not isinstance(existing, (KnowledgeOrganizationMerge, KnowledgeOrganizationConflict)):
        raise ValueError(t("organization reduction multi-source candidate missing effective action"))
    if isinstance(existing, KnowledgeOrganizationMerge):
        if not isinstance(plan_item, KnowledgeOrganizationMerge):
            raise ValueError(t("organization reduction cannot change an existing multi-source action"))
        if plan_item.primary_knowledge_id != existing.primary_knowledge_id or plan_item.target.knowledge_key != candidate.knowledge_key or plan_item.target.content != candidate.content:
            raise ValueError(t("organization reduction existing merge must remain unchanged"))
        return existing
    if not isinstance(plan_item, KnowledgeOrganizationConflict):
        raise ValueError(t("organization reduction cannot change an existing multi-source action"))
    return existing


def _compose_scope_plan(
    plan: KnowledgeOrganizationPlan,
    *,
    scope: tuple[KnowledgeOrganizationScopeItem, ...],
) -> tuple[KnowledgeOrganizationPlan, tuple[KnowledgeOrganizationScopeItem, ...]]:
    source_to_scope_index: dict[tuple[int, int], int] = {}
    for scope_index, scope_item in enumerate(scope):
        for source in scope_item.sources:
            if source in source_to_scope_index:
                raise ValueError(t("organization scope source duplicated across candidates"))
            source_to_scope_index[source] = scope_index

    effective_items: list[dict[str, Any]] = []
    output_scope: list[KnowledgeOrganizationScopeItem] = []
    covered_scope_indexes: set[int] = set()
    for plan_item in plan.items:
        refs = _source_refs(plan_item)
        ref_set = set(refs)
        touched_indexes: list[int] = []
        for source in refs:
            scope_index = source_to_scope_index.get(source)
            if scope_index is None:
                raise ValueError(t("organization reduction source missing"))
            if scope_index not in touched_indexes:
                touched_indexes.append(scope_index)

        touched = tuple(scope[index] for index in touched_indexes)
        for candidate in touched:
            if not set(candidate.sources).issubset(ref_set):
                raise ValueError(t("organization reduction cannot split a prior candidate"))
        if covered_scope_indexes.intersection(touched_indexes):
            raise ValueError(t("organization reduction candidate covered more than once"))
        covered_scope_indexes.update(touched_indexes)

        if len(touched) == 1 and ref_set != set(touched[0].sources):
            raise ValueError(t("organization reduction candidate coverage mismatch"))
        effective_item = _resolve_reduction_effective_item(plan_item, touched=touched)

        effective_items.append(effective_item.model_dump(mode="json"))
        output_scope.append(
            _build_reduction_output_scope_item(
                plan_item,
                touched=touched,
                effective_item=effective_item,
            )
        )

    if covered_scope_indexes != set(range(len(scope))):
        raise ValueError(t("organization reduction candidate coverage incomplete"))
    effective_plan = KnowledgeOrganizationPlan.model_validate({"items": effective_items})
    effective_plan = _validate_scope_plan(effective_plan, scope=scope)
    return effective_plan, tuple(output_scope)


def _scope_item_from_compact_descriptor(raw: Mapping[str, Any]) -> KnowledgeOrganizationScopeItem:
    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise RuntimeError(t("organization compact descriptor sources invalid"))
    sources: list[tuple[int, int]] = []
    for source in raw_sources:
        if not isinstance(source, list) or len(source) != 2 or not all(isinstance(value, int) and not isinstance(value, bool) for value in source):
            raise RuntimeError(t("organization compact descriptor source invalid"))
        sources.append((source[0], source[1]))

    raw_effective_item = raw.get("effective_item")
    if not isinstance(raw_effective_item, Mapping):
        raise RuntimeError(t("organization compact descriptor effective item invalid"))
    effective_plan = KnowledgeOrganizationPlan.model_validate({"items": [dict(raw_effective_item)]})
    if len(effective_plan.items) != 1 or set(_source_refs(effective_plan.items[0])) != set(sources):
        raise RuntimeError(t("organization compact descriptor effective item mismatch"))

    knowledge_key = raw.get("knowledge_key")
    content = raw.get("content")
    content_hash = raw.get("content_hash")
    source_type = raw.get("source_type")
    source_reference = raw.get("source_reference")
    if not all(isinstance(value, str) and value for value in (knowledge_key, content, content_hash, source_type)):
        raise RuntimeError(t("organization compact descriptor content invalid"))
    if source_reference is not None and not isinstance(source_reference, Mapping):
        raise RuntimeError(t("organization compact descriptor source reference invalid"))
    raw_related_ids = raw.get("related_ids") or []
    if not isinstance(raw_related_ids, list) or any(not isinstance(value, int) or isinstance(value, bool) for value in raw_related_ids):
        raise RuntimeError(t("organization compact descriptor related ids invalid"))
    return KnowledgeOrganizationScopeItem(
        sources=tuple(sources),
        knowledge_key=knowledge_key,
        content=content,
        content_hash=content_hash,
        source_type=source_type,
        source_reference=source_reference,
        related_ids=frozenset(raw_related_ids),
        effective_item=effective_plan.items[0],
    )


async def _iter_compact_scope_items(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> AsyncIterator[tuple[int, KnowledgeOrganizationScopeItem]]:
    async for fragment in _iter_stage_fragments(session_factory, stage=stage):
        raw_output_items = fragment.candidate_scope.get("output_items") if isinstance(fragment.candidate_scope, dict) else None
        if isinstance(raw_output_items, list):
            for raw in raw_output_items:
                if not isinstance(raw, Mapping):
                    raise RuntimeError(t("organization compact output descriptor invalid"))
                yield fragment.fragment_index, _scope_item_from_compact_descriptor(raw)
            continue

        raw_items = fragment.candidate_scope.get("items") if isinstance(fragment.candidate_scope, dict) else None
        if not isinstance(raw_items, list):
            raise RuntimeError(t("organization fragment candidate scope invalid"))
        descriptors: dict[int, Mapping[str, Any]] = {}
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                raise RuntimeError(t("organization fragment candidate descriptor invalid"))
            for source in raw.get("sources") or []:
                if isinstance(source, list) and len(source) == 2 and isinstance(source[0], int):
                    descriptors[source[0]] = raw
        plan = KnowledgeOrganizationPlan.model_validate(fragment.result)
        for plan_item in plan.items:
            yield fragment.fragment_index, _scope_item_from_plan_item(plan_item, descriptors=descriptors)


async def _iter_reduction_groups(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lower_stage: KnowledgeOrganizationStage,
    model: KnowledgeOrganizationModelConfig,
    first_index: int = 0,
) -> AsyncIterator[KnowledgeOrganizationFragmentInput]:
    current: list[KnowledgeOrganizationScopeItem] = []
    fragment_index = 0

    async def emit_current() -> AsyncIterator[KnowledgeOrganizationFragmentInput]:
        nonlocal fragment_index
        if not current:
            return
        group = tuple(current)
        current.clear()
        current_index = fragment_index
        fragment_index += 1
        if current_index >= first_index:
            yield KnowledgeOrganizationFragmentInput(
                fragment_index=current_index,
                scope=group,
                input_tokens=_scope_tokens(group),
            )

    async for _lower_fragment_index, item in _iter_compact_scope_items(session_factory, stage=lower_stage):
        candidate = tuple([*current, item])
        if current and _scope_tokens(candidate) > model.input_budget_tokens:
            async for group in emit_current():
                yield group
        current.append(item)
    async for group in emit_current():
        yield group


async def _combine_stage_plan(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> KnowledgeOrganizationPlan:
    items: list[dict[str, Any]] = []
    async for fragment in _iter_stage_fragments(session_factory, stage=stage):
        plan = KnowledgeOrganizationPlan.model_validate(fragment.result)
        items.extend(item.model_dump(mode="json") for item in plan.items)
    return KnowledgeOrganizationPlan.model_validate({"items": items})


async def _measure_stage_output_tokens(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> int:
    total = 0
    async for _fragment_index, item in _iter_compact_scope_items(session_factory, stage=stage):
        total += _scope_item_tokens(item)
    return total


async def _measure_reduction_input_tokens(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lower_stage: KnowledgeOrganizationStage,
) -> int:
    total = 0
    async for _fragment_index, item in _iter_compact_scope_items(session_factory, stage=lower_stage):
        total += _scope_item_tokens(item)
    return total


async def _validate_reduction_decrease(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lower_stage: KnowledgeOrganizationStage | None,
    output_tokens: int,
) -> None:
    if lower_stage is None:
        return
    lower_input_tokens = await _measure_reduction_input_tokens(session_factory, lower_stage=lower_stage)
    if output_tokens >= lower_input_tokens:
        raise KnowledgeOrganizationNotConvergedError(
            cause=f"input_tokens={lower_input_tokens}, output_tokens={output_tokens}",
        )
