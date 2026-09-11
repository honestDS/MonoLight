from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    KNOWLEDGE_ORGANIZATION_CONTEXT_SAFETY_MARGIN_TOKENS,
    KNOWLEDGE_ORGANIZATION_SEMANTIC_NEIGHBOR_COUNT,
    KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS,
    MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS,
    MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
)
from app.core.crud.channel.channel import channel_crud
from app.core.crud.memory.store import memory_store_crud
from app.core.i18n import t
from app.core.knowledge.managed import normalize_managed_knowledge_key
from app.core.knowledge.organization_types import (
    KnowledgeOrganizationConflict,
    KnowledgeOrganizationKeep,
    KnowledgeOrganizationMerge,
    KnowledgeOrganizationPlan,
    KnowledgeOrganizationPlanItem,
    KnowledgeOrganizationUpdate,
)
from app.core.prompts import KNOWLEDGE_ORGANIZATION_SYSTEM_PROMPT
from app.core.utils.tokenizer import estimate_tokens, truncate_text_to_tokens
from app.models.channel import ChannelModelItem, ModelUsage, is_channel_model_available, resolve_model_protocol
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient
from app.providers.vector.chroma import async_get_collection_items_by_ids, async_query_collection


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationModelConfig:
    channel_id: int
    channel_name: str
    model_id: str
    protocol: str
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    http_proxy: str | None = field(default=None, repr=False)
    custom_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    temperature: float = 0.1
    top_p: float | None = None
    timeout: float = 600.0
    context_window_tokens: int = 0
    max_output_tokens: int = 0
    safety_margin_tokens: int = 0
    system_prompt_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "custom_headers", MappingProxyType(dict(self.custom_headers)))

    @property
    def input_budget_tokens(self) -> int:
        return max(
            0,
            self.context_window_tokens - self.max_output_tokens - self.safety_margin_tokens - self.system_prompt_tokens,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationCandidate:
    knowledge_id: int
    expected_version: int
    knowledge_key: str
    content: str
    content_hash: str
    content_token_count: int
    source_type: str
    source_reference: Mapping[str, Any] | None = None
    vector_item_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.source_reference is not None:
            object.__setattr__(self, "source_reference", MappingProxyType(dict(self.source_reference)))
        object.__setattr__(self, "vector_item_ids", tuple(self.vector_item_ids))


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationFragmentGroup:
    fragment_index: int
    candidates: tuple[KnowledgeOrganizationCandidate, ...]
    input_tokens: int


def _key_terms(value: str) -> frozenset[str]:
    normalized = value.casefold().replace("_", "-")
    return frozenset(term for term in re.split(r"[^\w\u4e00-\u9fff]+", normalized) if term)


def _same_source(left: KnowledgeOrganizationCandidate, right: KnowledgeOrganizationCandidate) -> bool:
    return left.source_reference is not None and right.source_reference is not None and canonical_json_dumps(dict(left.source_reference)) == canonical_json_dumps(dict(right.source_reference))


def _key_related(left: KnowledgeOrganizationCandidate, right: KnowledgeOrganizationCandidate) -> bool:
    left_terms = _key_terms(left.knowledge_key)
    right_terms = _key_terms(right.knowledge_key)
    if not left_terms or not right_terms:
        return False
    shared = left_terms & right_terms
    return bool(shared) and len(shared) * 2 >= min(len(left_terms), len(right_terms))


def _candidate_input_tokens(candidate: KnowledgeOrganizationCandidate) -> int:
    payload = {
        "knowledge_id": candidate.knowledge_id,
        "expected_version": candidate.expected_version,
        "knowledge_key": candidate.knowledge_key,
        "content": candidate.content,
        "content_hash": candidate.content_hash,
        "content_token_count": candidate.content_token_count,
        "source_type": candidate.source_type,
        "source_reference": dict(candidate.source_reference) if candidate.source_reference is not None else None,
    }
    return max(1, estimate_tokens(canonical_json_dumps(payload)))


def build_semantic_fragment_groups(
    candidates: tuple[KnowledgeOrganizationCandidate, ...],
    *,
    model: KnowledgeOrganizationModelConfig,
    semantic_neighbors: Mapping[int, set[int] | frozenset[int]] | None = None,
) -> tuple[KnowledgeOrganizationFragmentGroup, ...]:
    if not candidates:
        return ()
    neighbors = semantic_neighbors or {}
    by_id = {candidate.knowledge_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError(t("duplicate knowledge candidate"))

    parent = {candidate.knowledge_id: candidate.knowledge_id for candidate in candidates}

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    ordered = tuple(sorted(candidates, key=lambda candidate: candidate.knowledge_id))
    for index, left in enumerate(ordered):
        explicit_neighbors = set(neighbors.get(left.knowledge_id, ()))
        for right in ordered[index + 1 :]:
            if right.knowledge_id in explicit_neighbors or left.knowledge_id in set(neighbors.get(right.knowledge_id, ())) or _same_source(left, right) or _key_related(left, right):
                union(left.knowledge_id, right.knowledge_id)

    components: dict[int, list[KnowledgeOrganizationCandidate]] = {}
    for candidate in ordered:
        components.setdefault(find(candidate.knowledge_id), []).append(candidate)

    groups: list[KnowledgeOrganizationFragmentGroup] = []
    input_budget = max(1, model.input_budget_tokens)
    for root in sorted(components):
        current: list[KnowledgeOrganizationCandidate] = []
        current_tokens = 0
        for candidate in components[root]:
            candidate_tokens = _candidate_input_tokens(candidate)
            if current and current_tokens + candidate_tokens > input_budget:
                groups.append(
                    KnowledgeOrganizationFragmentGroup(
                        fragment_index=len(groups),
                        candidates=tuple(current),
                        input_tokens=current_tokens,
                    )
                )
                current = []
                current_tokens = 0
            current.append(candidate)
            current_tokens += candidate_tokens
        if current:
            groups.append(
                KnowledgeOrganizationFragmentGroup(
                    fragment_index=len(groups),
                    candidates=tuple(current),
                    input_tokens=current_tokens,
                )
            )
    return tuple(groups)


def split_content_for_analysis(content: str, *, max_tokens: int) -> tuple[str, ...]:
    if not isinstance(content, str) or not content or max_tokens < 1:
        raise ValueError(t("invalid analysis split"))
    remaining = content
    parts: list[str] = []
    while remaining:
        prefix, truncated = truncate_text_to_tokens(remaining, max_tokens)
        if not prefix:
            raise ValueError(t("analysis split made no progress"))
        parts.append(prefix)
        remaining = remaining[len(prefix) :]
        if not truncated:
            break
    if "".join(parts) != content:
        raise ValueError(t("analysis split lost content"))
    return tuple(parts)


def _source_refs(item: KnowledgeOrganizationPlanItem) -> tuple[tuple[int, int], ...]:
    if isinstance(item, (KnowledgeOrganizationKeep, KnowledgeOrganizationUpdate)):
        return ((item.source.knowledge_id, item.source.expected_version),)
    if isinstance(item, (KnowledgeOrganizationMerge, KnowledgeOrganizationConflict)):
        return tuple((source.knowledge_id, source.expected_version) for source in item.sources)
    raise TypeError(type(item).__name__)


def validate_knowledge_organization_scope_plan(
    plan: KnowledgeOrganizationPlan,
    *,
    allowed_versions: Mapping[int, int],
    original_keys: Mapping[int, str],
    original_hashes: Mapping[int, str],
) -> KnowledgeOrganizationPlan:
    if set(allowed_versions) != set(original_keys) or set(allowed_versions) != set(original_hashes):
        raise ValueError(t("organization validation scope invalid"))

    normalized_original_keys = {knowledge_id: normalize_managed_knowledge_key(knowledge_key) for knowledge_id, knowledge_key in original_keys.items()}
    covered: set[int] = set()
    target_keys: set[str] = set()
    target_hashes: set[str] = set()

    for item in plan.items:
        refs = _source_refs(item)
        item_ids: set[int] = set()
        for knowledge_id, expected_version in refs:
            if allowed_versions.get(knowledge_id) != expected_version or knowledge_id in covered or knowledge_id in item_ids:
                raise ValueError(t("organization source mismatch"))
            item_ids.add(knowledge_id)
        covered.update(item_ids)

        summary = item.summary
        if not summary.strip() or estimate_tokens(summary) > KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS:
            raise ValueError(t("organization summary invalid"))

        if isinstance(item, KnowledgeOrganizationMerge) and item.primary_knowledge_id not in item_ids:
            raise ValueError(t("organization merge primary invalid"))

        if isinstance(item, (KnowledgeOrganizationUpdate, KnowledgeOrganizationMerge)):
            normalized_key = normalize_managed_knowledge_key(item.target.knowledge_key)
            if not item.target.content.strip() or estimate_tokens(item.target.content) > MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS:
                raise ValueError(t("organization target invalid"))
            content_hash = hashlib.sha256(item.target.content.encode("utf-8")).hexdigest()
            if normalized_key in target_keys or content_hash in target_hashes:
                raise ValueError(t("organization target duplicate"))
            for other_id, existing_key in normalized_original_keys.items():
                if other_id not in item_ids and existing_key == normalized_key:
                    raise ValueError(t("organization target key conflict"))
            for other_id, existing_hash in original_hashes.items():
                if other_id not in item_ids and existing_hash == content_hash:
                    raise ValueError(t("organization target content conflict"))
            target_keys.add(normalized_key)
            target_hashes.add(content_hash)

    if covered != set(allowed_versions):
        raise ValueError(t("organization source coverage invalid"))
    return plan


def validate_knowledge_organization_plan(
    plan: KnowledgeOrganizationPlan,
    *,
    candidates: tuple[KnowledgeOrganizationCandidate, ...],
) -> KnowledgeOrganizationPlan:
    allowed = {candidate.knowledge_id: candidate for candidate in candidates}
    if len(allowed) != len(candidates):
        raise ValueError(t("duplicate knowledge candidate"))
    return validate_knowledge_organization_scope_plan(
        plan,
        allowed_versions={knowledge_id: candidate.expected_version for knowledge_id, candidate in allowed.items()},
        original_keys={knowledge_id: candidate.knowledge_key for knowledge_id, candidate in allowed.items()},
        original_hashes={knowledge_id: candidate.content_hash for knowledge_id, candidate in allowed.items()},
    )


def _build_model_config(channel, item: ChannelModelItem) -> KnowledgeOrganizationModelConfig:
    if item.context_window_k is None or item.max_tokens is None:
        raise ValueError(t("organization model budget missing"))
    return KnowledgeOrganizationModelConfig(
        channel_id=channel.id,
        channel_name=channel.name,
        model_id=item.model_id,
        protocol=resolve_model_protocol({"protocol": item.protocol.value}),
        base_url=channel.base_url,
        api_key=channel.get_decrypted_api_key(),
        http_proxy=channel.http_proxy,
        custom_headers=item.advanced_settings.custom_headers,
        temperature=item.temperature if item.temperature is not None else 0.1,
        top_p=item.top_p,
        timeout=MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
        context_window_tokens=item.context_window_k * CONTEXT_WINDOW_TOKENS_PER_K,
        max_output_tokens=item.max_tokens,
        safety_margin_tokens=KNOWLEDGE_ORGANIZATION_CONTEXT_SAFETY_MARGIN_TOKENS,
        system_prompt_tokens=estimate_tokens(KNOWLEDGE_ORGANIZATION_SYSTEM_PROMPT),
    )


async def load_knowledge_organization_model_candidates(
    db: AsyncSession,
    *,
    uid: str,
) -> tuple[KnowledgeOrganizationModelConfig, ...]:
    store = await memory_store_crud.get_snapshot_by_uid(db, uid=uid)
    if store is None or store.organization_channel_id is None or not store.organization_model_id:
        raise ValueError(t("organization model not configured"))
    channel = await channel_crud.get(db, store.organization_channel_id)
    if channel is None or channel.id is None or channel.is_active is not True or not channel.base_url:
        raise ValueError(t("organization channel unavailable"))

    parsed: list[ChannelModelItem] = []
    for raw in channel.model_ids or []:
        try:
            item = ChannelModelItem.model_validate(raw)
        except Exception:
            continue
        if item.usage != ModelUsage.CHAT or not is_channel_model_available(item):
            continue
        if item.context_window_k is None or item.max_tokens is None:
            continue
        parsed.append(item)
    primary = next((item for item in parsed if item.model_id == store.organization_model_id), None)
    if primary is None:
        raise ValueError(t("organization primary model unavailable"))
    ordered = [primary, *(item for item in parsed if item.model_id != primary.model_id)]
    return tuple(_build_model_config(channel, item) for item in ordered)


def _candidate_request_payload(candidate: KnowledgeOrganizationCandidate) -> dict[str, Any]:
    return {
        "knowledge_id": candidate.knowledge_id,
        "expected_version": candidate.expected_version,
        "knowledge_key": candidate.knowledge_key,
        "content": candidate.content,
        "content_hash": candidate.content_hash,
        "content_token_count": candidate.content_token_count,
        "source_type": candidate.source_type,
        "source_reference": dict(candidate.source_reference) if candidate.source_reference is not None else None,
    }


async def call_knowledge_organization_model(
    model: KnowledgeOrganizationModelConfig,
    *,
    candidates: tuple[KnowledgeOrganizationCandidate, ...],
) -> KnowledgeOrganizationPlan:
    payload = canonical_json_dumps([_candidate_request_payload(candidate) for candidate in candidates])
    if estimate_tokens(payload) > model.input_budget_tokens:
        raise ValueError(t("organization candidate scope exceeds model budget"))
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
        raw = json.loads(content)
        plan = KnowledgeOrganizationPlan.model_validate(raw)
    except Exception as exc:
        raise ValueError(t("organization model result invalid")) from exc
    return validate_knowledge_organization_plan(plan, candidates=candidates)


def _average_embeddings(embeddings: Any) -> list[float] | None:
    if not isinstance(embeddings, list) or not embeddings:
        return None
    vectors = [vector for vector in embeddings if isinstance(vector, (list, tuple)) and vector]
    if not vectors:
        return None
    dimension = len(vectors[0])
    if dimension == 0 or any(len(vector) != dimension for vector in vectors):
        return None
    return [sum(float(vector[index]) for vector in vectors) / len(vectors) for index in range(dimension)]


async def load_vector_semantic_neighbors(
    candidates: tuple[KnowledgeOrganizationCandidate, ...],
    *,
    collection_name: str,
) -> dict[int, frozenset[int]]:
    allowed_versions = {candidate.knowledge_id: candidate.expected_version for candidate in candidates}
    mutable: dict[int, set[int]] = {candidate.knowledge_id: set() for candidate in candidates}
    for candidate in candidates:
        if not candidate.vector_item_ids:
            continue
        raw = await async_get_collection_items_by_ids(
            collection_name,
            candidate.vector_item_ids,
            include=["embeddings"],
        )
        representative = _average_embeddings(raw.get("embeddings") if isinstance(raw, Mapping) else None)
        if representative is None:
            continue
        queried = await async_query_collection(
            collection_name,
            representative,
            n_results=KNOWLEDGE_ORGANIZATION_SEMANTIC_NEIGHBOR_COUNT,
            include=["metadatas"],
        )
        metadata_groups = queried.get("metadatas") if isinstance(queried, Mapping) else None
        metadatas = metadata_groups[0] if isinstance(metadata_groups, list) and metadata_groups else []
        for metadata in metadatas or []:
            if not isinstance(metadata, Mapping):
                continue
            neighbor_id = metadata.get("managed_knowledge_id")
            neighbor_version = metadata.get("managed_knowledge_version")
            if isinstance(neighbor_id, int) and not isinstance(neighbor_id, bool) and neighbor_id != candidate.knowledge_id and allowed_versions.get(neighbor_id) == neighbor_version:
                mutable[candidate.knowledge_id].add(neighbor_id)
                mutable[neighbor_id].add(candidate.knowledge_id)
    return {knowledge_id: frozenset(values) for knowledge_id, values in mutable.items()}


__all__ = [
    "KnowledgeOrganizationCandidate",
    "KnowledgeOrganizationFragmentGroup",
    "KnowledgeOrganizationModelConfig",
    "build_semantic_fragment_groups",
    "call_knowledge_organization_model",
    "load_knowledge_organization_model_candidates",
    "load_vector_semantic_neighbors",
    "split_content_for_analysis",
    "validate_knowledge_organization_plan",
    "validate_knowledge_organization_scope_plan",
]
