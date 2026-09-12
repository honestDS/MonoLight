from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable, Mapping
from tempfile import TemporaryFile
from typing import Any, TextIO

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE
from app.core.i18n import t
from app.core.knowledge import organization_pipeline
from app.core.knowledge.organization import load_knowledge_organization_snapshot_item_page
from app.core.knowledge.organization_runtime import (
    KnowledgeOrganizationCandidate,
    KnowledgeOrganizationModelConfig,
    validate_knowledge_organization_scope_plan,
)
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
from app.core.utils.tokenizer import estimate_tokens
from app.models.knowledge_base import KnowledgeOrganizationSnapshot

__all__ = []


def _terms(value: str) -> frozenset[str]:
    normalized = value.casefold().replace("_", "-")
    return frozenset(term for term in re.split(r"[^\w\u4e00-\u9fff]+", normalized) if term)


def _same_source(left: KnowledgeOrganizationScopeItem, right: KnowledgeOrganizationScopeItem) -> bool:
    return left.source_reference is not None and right.source_reference is not None and canonical_json_dumps(dict(left.source_reference)) == canonical_json_dumps(dict(right.source_reference))


def _scope_related(left: KnowledgeOrganizationScopeItem, right: KnowledgeOrganizationScopeItem) -> bool:
    if left.source_ids & right.related_ids or right.source_ids & left.related_ids:
        return True
    if _same_source(left, right):
        return True
    left_key_terms = _terms(left.knowledge_key)
    right_key_terms = _terms(right.knowledge_key)
    if left_key_terms and right_key_terms and left_key_terms == right_key_terms:
        return True
    left_content_terms = _terms(left.content)
    right_content_terms = _terms(right.content)
    shared = left_content_terms & right_content_terms
    return bool(shared) and len(shared) >= 2


def _scope_payload(item: KnowledgeOrganizationScopeItem) -> dict[str, Any]:
    payload = {
        "sources": [{"knowledge_id": knowledge_id, "expected_version": expected_version} for knowledge_id, expected_version in item.sources],
        "knowledge_key": item.knowledge_key,
        "content": item.content,
        "content_hash": item.content_hash,
        "source_type": item.source_type,
        "source_reference": dict(item.source_reference) if item.source_reference is not None else None,
    }
    if item.effective_item is not None:
        payload["current_action"] = item.effective_item.action
    return payload


def _scope_descriptor(item: KnowledgeOrganizationScopeItem) -> dict[str, Any]:
    return {
        "sources": [[knowledge_id, expected_version] for knowledge_id, expected_version in item.sources],
        "knowledge_key": item.knowledge_key,
        "content_hash": item.content_hash,
        "source_type": item.source_type,
        "source_reference": dict(item.source_reference) if item.source_reference is not None else None,
        "related_ids": sorted(item.related_ids),
    }


def _compact_scope_descriptor(item: KnowledgeOrganizationScopeItem) -> dict[str, Any]:
    return {
        **_scope_descriptor(item),
        "content": item.content,
        "effective_item": item.effective_item.model_dump(mode="json") if item.effective_item is not None else None,
    }


def _scope_item_tokens(item: KnowledgeOrganizationScopeItem) -> int:
    return max(1, estimate_tokens(canonical_json_dumps(_scope_payload(item))))


def _scope_tokens(scope: tuple[KnowledgeOrganizationScopeItem, ...]) -> int:
    return max(1, estimate_tokens(canonical_json_dumps([_scope_payload(item) for item in scope])))


def _plan_tokens(plan: KnowledgeOrganizationPlan) -> int:
    return max(1, estimate_tokens(canonical_json_dumps(plan.model_dump(mode="json"))))


def _source_refs(item: KnowledgeOrganizationPlanItem) -> tuple[tuple[int, int], ...]:
    if isinstance(item, (KnowledgeOrganizationKeep, KnowledgeOrganizationUpdate)):
        return ((item.source.knowledge_id, item.source.expected_version),)
    if isinstance(item, (KnowledgeOrganizationMerge, KnowledgeOrganizationConflict)):
        return tuple((source.knowledge_id, source.expected_version) for source in item.sources)
    raise TypeError(type(item).__name__)


def _validate_scope_plan(
    plan: KnowledgeOrganizationPlan,
    *,
    scope: tuple[KnowledgeOrganizationScopeItem, ...],
) -> KnowledgeOrganizationPlan:
    allowed_versions: dict[int, int] = {}
    original_keys: dict[int, str] = {}
    original_hashes: dict[int, str] = {}
    for scope_item in scope:
        for knowledge_id, expected_version in scope_item.sources:
            if knowledge_id in allowed_versions and allowed_versions[knowledge_id] != expected_version:
                raise ValueError(t("organization scope version conflict"))
            allowed_versions[knowledge_id] = expected_version
            original_keys.setdefault(knowledge_id, scope_item.knowledge_key)
            original_hashes.setdefault(knowledge_id, scope_item.content_hash)

    return validate_knowledge_organization_scope_plan(
        plan,
        allowed_versions=allowed_versions,
        original_keys=original_keys,
        original_hashes=original_hashes,
    )


def _scope_from_snapshot_item(item: Mapping[str, Any]) -> KnowledgeOrganizationScopeItem:
    knowledge_id = item["knowledge_id"]
    expected_version = item["expected_version"]
    content = item["content"]
    return KnowledgeOrganizationScopeItem(
        sources=((knowledge_id, expected_version),),
        knowledge_key=item["knowledge_key"],
        content=content,
        content_hash=item["content_hash"],
        source_type=str(item["source_type"]),
        source_reference=item.get("source_reference"),
        vector_item_ids=tuple(item.get("vector_item_ids") or ()),
    )


def _with_related_ids(
    item: KnowledgeOrganizationScopeItem,
    related_ids: set[int] | frozenset[int],
) -> KnowledgeOrganizationScopeItem:
    return KnowledgeOrganizationScopeItem(
        sources=item.sources,
        knowledge_key=item.knowledge_key,
        content=item.content,
        content_hash=item.content_hash,
        source_type=item.source_type,
        source_reference=item.source_reference,
        vector_item_ids=item.vector_item_ids,
        related_ids=frozenset(related_ids),
        effective_item=item.effective_item,
    )


def _group_scope_page(
    items: tuple[KnowledgeOrganizationScopeItem, ...],
    *,
    model: KnowledgeOrganizationModelConfig,
) -> tuple[tuple[KnowledgeOrganizationScopeItem, ...], ...]:
    if not items:
        return ()
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for left_index, left in enumerate(items):
        for right_index in range(left_index + 1, len(items)):
            if _scope_related(left, items[right_index]):
                union(left_index, right_index)

    components: dict[int, list[KnowledgeOrganizationScopeItem]] = {}
    for index, item in enumerate(items):
        components.setdefault(find(index), []).append(item)

    groups: list[tuple[KnowledgeOrganizationScopeItem, ...]] = []
    for root in sorted(components):
        current: list[KnowledgeOrganizationScopeItem] = []
        for item in components[root]:
            candidate = tuple([*current, item])
            if current and _scope_tokens(candidate) > model.input_budget_tokens:
                groups.append(tuple(current))
                current = []
            current.append(item)
        if current:
            groups.append(tuple(current))
    return tuple(groups)


async def _iter_snapshot_scope_items(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
) -> AsyncIterator[KnowledgeOrganizationScopeItem]:
    expected_sequence = 0
    while expected_sequence < snapshot.item_count:
        async with session_factory() as db:
            page = await load_knowledge_organization_snapshot_item_page(
                db,
                snapshot=snapshot,
                after_sequence=expected_sequence - 1,
                limit=KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE,
            )
        if not page:
            raise RuntimeError(t("organization snapshot item page missing"))
        for item in page:
            yield _scope_from_snapshot_item(item)
            expected_sequence += 1


async def _try_load_single_request_scope(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    model: KnowledgeOrganizationModelConfig,
) -> tuple[KnowledgeOrganizationScopeItem, ...] | None:
    scope: list[KnowledgeOrganizationScopeItem] = []
    async for item in _iter_snapshot_scope_items(session_factory, snapshot=snapshot):
        scope.append(item)
        if _scope_tokens(tuple(scope)) > model.input_budget_tokens:
            return None
    return tuple(scope)


async def _load_semantic_neighbors(
    loader: Callable[..., Any],
    items: tuple[KnowledgeOrganizationScopeItem, ...],
    *,
    collection_name: str,
) -> Mapping[int, set[int] | frozenset[int]]:
    candidates = tuple(
        KnowledgeOrganizationCandidate(
            knowledge_id=item.sources[0][0],
            expected_version=item.sources[0][1],
            knowledge_key=item.knowledge_key,
            content=item.content,
            content_hash=item.content_hash,
            content_token_count=max(1, estimate_tokens(item.content)),
            source_type=item.source_type,
            source_reference=item.source_reference,
            vector_item_ids=item.vector_item_ids,
        )
        for item in items
        if len(item.sources) == 1
    )
    if not candidates:
        return {}
    loaded = await organization_pipeline.maybe_await(loader(candidates, collection_name))
    return loaded if isinstance(loaded, Mapping) else {}


async def _iter_initial_groups(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    model: KnowledgeOrganizationModelConfig,
    collection_name: str,
    semantic_neighbor_loader: Callable[..., Any],
    first_index: int = 0,
) -> AsyncIterator[KnowledgeOrganizationFragmentInput]:
    page: list[KnowledgeOrganizationScopeItem] = []
    fragment_index = 0

    async def emit_page() -> AsyncIterator[KnowledgeOrganizationFragmentInput]:
        nonlocal fragment_index
        if not page:
            return
        page_items = tuple(page)
        page.clear()
        oversized = [item for item in page_items if _scope_tokens((item,)) > model.input_budget_tokens]
        regular = tuple(item for item in page_items if _scope_tokens((item,)) <= model.input_budget_tokens)
        if regular:
            neighbors = await _load_semantic_neighbors(
                semantic_neighbor_loader,
                regular,
                collection_name=collection_name,
            )
            enriched = tuple(_with_related_ids(item, neighbors.get(item.sources[0][0], frozenset())) for item in regular)
            for group in _group_scope_page(enriched, model=model):
                current_index = fragment_index
                fragment_index += 1
                if current_index >= first_index:
                    yield KnowledgeOrganizationFragmentInput(
                        fragment_index=current_index,
                        scope=group,
                        input_tokens=_scope_tokens(group),
                    )
        for item in oversized:
            current_index = fragment_index
            fragment_index += 1
            if current_index >= first_index:
                yield KnowledgeOrganizationFragmentInput(
                    fragment_index=current_index,
                    scope=(item,),
                    input_tokens=_scope_tokens((item,)),
                )

    async for item in _iter_snapshot_scope_items(session_factory, snapshot=snapshot):
        page.append(item)
        if len(page) >= KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE:
            async for group in emit_page():
                yield group
    async for group in emit_page():
        yield group


async def _count_groups(groups: AsyncIterator[KnowledgeOrganizationFragmentInput]) -> int:
    count = 0
    async for group in groups:
        if group.fragment_index != count:
            raise RuntimeError(t("organization group index mismatch"))
        count += 1
    return count


def _initial_group_spool_payload(group: KnowledgeOrganizationFragmentInput) -> dict[str, Any]:
    return {
        "fragment_index": group.fragment_index,
        "input_tokens": group.input_tokens,
        "scope": [
            {
                "sources": [[knowledge_id, expected_version] for knowledge_id, expected_version in item.sources],
                "knowledge_key": item.knowledge_key,
                "content": item.content,
                "content_hash": item.content_hash,
                "source_type": item.source_type,
                "source_reference": dict(item.source_reference) if item.source_reference is not None else None,
                "vector_item_ids": list(item.vector_item_ids),
                "related_ids": sorted(item.related_ids),
            }
            for item in group.scope
        ],
    }


def _initial_group_from_spool_payload(raw: Mapping[str, Any]) -> KnowledgeOrganizationFragmentInput:
    fragment_index = raw.get("fragment_index")
    input_tokens = raw.get("input_tokens")
    raw_scope = raw.get("scope")
    if not isinstance(fragment_index, int) or isinstance(fragment_index, bool) or fragment_index < 0 or not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 1 or not isinstance(raw_scope, list) or not raw_scope:
        raise RuntimeError(t("organization initial group spool invalid"))

    scope: list[KnowledgeOrganizationScopeItem] = []
    for raw_item in raw_scope:
        if not isinstance(raw_item, Mapping):
            raise RuntimeError(t("organization initial group spool invalid"))
        raw_sources = raw_item.get("sources")
        raw_vector_item_ids = raw_item.get("vector_item_ids")
        raw_related_ids = raw_item.get("related_ids")
        source_reference = raw_item.get("source_reference")
        if (
            not isinstance(raw_sources, list)
            or not raw_sources
            or not isinstance(raw_vector_item_ids, list)
            or any(not isinstance(value, str) or not value for value in raw_vector_item_ids)
            or not isinstance(raw_related_ids, list)
            or any(not isinstance(value, int) or isinstance(value, bool) for value in raw_related_ids)
            or (source_reference is not None and not isinstance(source_reference, Mapping))
        ):
            raise RuntimeError(t("organization initial group spool invalid"))
        sources: list[tuple[int, int]] = []
        for source in raw_sources:
            if not isinstance(source, list) or len(source) != 2 or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in source):
                raise RuntimeError(t("organization initial group spool invalid"))
            sources.append((source[0], source[1]))
        knowledge_key = raw_item.get("knowledge_key")
        content = raw_item.get("content")
        content_hash = raw_item.get("content_hash")
        source_type = raw_item.get("source_type")
        if not all(isinstance(value, str) and value for value in (knowledge_key, content, content_hash, source_type)):
            raise RuntimeError(t("organization initial group spool invalid"))
        scope.append(
            KnowledgeOrganizationScopeItem(
                sources=tuple(sources),
                knowledge_key=knowledge_key,
                content=content,
                content_hash=content_hash,
                source_type=source_type,
                source_reference=source_reference,
                vector_item_ids=tuple(raw_vector_item_ids),
                related_ids=frozenset(raw_related_ids),
            )
        )
    return KnowledgeOrganizationFragmentInput(
        fragment_index=fragment_index,
        scope=tuple(scope),
        input_tokens=input_tokens,
    )


async def _capture_initial_groups(
    groups: AsyncIterator[KnowledgeOrganizationFragmentInput],
) -> tuple[TextIO, int]:
    spool = TemporaryFile(mode="w+t", encoding="utf-8", newline="\n")
    count = 0
    try:
        async for group in groups:
            if group.fragment_index != count:
                raise RuntimeError(t("organization group index mismatch"))
            spool.write(canonical_json_dumps(_initial_group_spool_payload(group)))
            spool.write("\n")
            count += 1
        spool.flush()
        spool.seek(0)
        return spool, count
    except BaseException:
        spool.close()
        raise


async def _iter_captured_initial_groups(
    spool: TextIO,
    *,
    first_index: int,
) -> AsyncIterator[KnowledgeOrganizationFragmentInput]:
    spool.seek(0)
    expected_index = 0
    for line in spool:
        try:
            raw = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(t("organization initial group spool invalid")) from exc
        if not isinstance(raw, Mapping):
            raise RuntimeError(t("organization initial group spool invalid"))
        group = _initial_group_from_spool_payload(raw)
        if group.fragment_index != expected_index:
            raise RuntimeError(t("organization group index mismatch"))
        expected_index += 1
        if group.fragment_index >= first_index:
            yield group
