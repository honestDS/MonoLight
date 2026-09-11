from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import (
    KNOWLEDGE_ORGANIZATION_FRAGMENT_CONCURRENCY,
    KNOWLEDGE_ORGANIZATION_INPUT_QUEUE_CAPACITY,
    KNOWLEDGE_ORGANIZATION_MODEL_MAX_ATTEMPTS,
    KNOWLEDGE_ORGANIZATION_REORDER_WINDOW,
    KNOWLEDGE_ORGANIZATION_RESULT_QUEUE_CAPACITY,
    KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE,
    KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS,
    LOG_KNOWLEDGE_ORGANIZATION_CONFIG_INVALID,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.organization import knowledge_organization_fragment_crud, knowledge_organization_stage_crud
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.i18n import t
from app.core.knowledge.organization import create_knowledge_organization_snapshot, load_knowledge_organization_snapshot_item_page
from app.core.knowledge.organization_runtime import (
    KnowledgeOrganizationCandidate,
    KnowledgeOrganizationModelConfig,
    load_knowledge_organization_model_candidates,
    load_vector_semantic_neighbors,
    split_content_for_analysis,
    validate_knowledge_organization_scope_plan,
)
from app.core.knowledge.organization_types import (
    KnowledgeOrganizationAnalysisResult,
    KnowledgeOrganizationConflict,
    KnowledgeOrganizationKeep,
    KnowledgeOrganizationMerge,
    KnowledgeOrganizationPlan,
    KnowledgeOrganizationPlanItem,
    KnowledgeOrganizationUpdate,
)
from app.core.log import get_logger
from app.core.prompts import KNOWLEDGE_ORGANIZATION_ANALYSIS_SYSTEM_PROMPT, KNOWLEDGE_ORGANIZATION_SYSTEM_PROMPT
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

logger = get_logger(__name__)


class KnowledgeOrganizationExecutionError(RuntimeError):
    code = "knowledge_organization_failed"


class KnowledgeOrganizationContextExceededError(KnowledgeOrganizationExecutionError):
    code = "organization_context_exceeded"


class KnowledgeOrganizationModelFailedError(KnowledgeOrganizationExecutionError):
    code = "organization_model_execution_failed"


class KnowledgeOrganizationNotConvergedError(KnowledgeOrganizationExecutionError):
    code = "organization_not_converged"


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationScopeItem:
    sources: tuple[tuple[int, int], ...]
    knowledge_key: str
    content: str
    content_hash: str
    source_type: str
    source_reference: Mapping[str, Any] | None = None
    vector_item_ids: tuple[str, ...] = ()
    related_ids: frozenset[int] = frozenset()
    effective_item: KnowledgeOrganizationPlanItem | None = None

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError(t("organization scope item requires sources"))
        if len({knowledge_id for knowledge_id, _version in self.sources}) != len(self.sources):
            raise ValueError(t("organization scope item has duplicate sources"))
        if self.source_reference is not None:
            object.__setattr__(self, "source_reference", MappingProxyType(dict(self.source_reference)))
        object.__setattr__(self, "vector_item_ids", tuple(self.vector_item_ids))
        object.__setattr__(self, "related_ids", frozenset(self.related_ids))

    @property
    def source_ids(self) -> frozenset[int]:
        return frozenset(knowledge_id for knowledge_id, _version in self.sources)


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationFragmentInput:
    fragment_index: int
    scope: tuple[KnowledgeOrganizationScopeItem, ...]
    input_tokens: int


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationFragmentResult:
    fragment_index: int
    scope: tuple[KnowledgeOrganizationScopeItem, ...]
    plan: KnowledgeOrganizationPlan
    output_scope: tuple[KnowledgeOrganizationScopeItem, ...]
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationPipelineStats:
    max_active_tasks: int
    max_input_queue_size: int
    max_result_queue_size: int
    max_reorder_size: int


@dataclass(frozen=True, slots=True)
class KnowledgeOrganizationExecutionResult:
    plan: KnowledgeOrganizationPlan
    model_id: str | None
    stage_count: int
    snapshot_id: int | None


@dataclass(frozen=True, slots=True)
class _StageExecutionResult:
    stage: KnowledgeOrganizationStage
    input_tokens: int
    output_tokens: int
    stats: KnowledgeOrganizationPipelineStats | None = None


def _item_index(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    fragment_index = getattr(value, "fragment_index", None)
    if isinstance(fragment_index, int) and not isinstance(fragment_index, bool):
        return fragment_index
    if isinstance(value, tuple) and value and isinstance(value[0], int) and not isinstance(value[0], bool):
        return value[0]
    raise ValueError(t("organization pipeline item has no integer index"))


async def run_bounded_knowledge_organization_pipeline[InputT, ResultT](
    *,
    inputs: AsyncIterator[InputT],
    expected_count: int,
    process: Callable[[InputT], Awaitable[ResultT]],
    persist: Callable[[ResultT], Awaitable[None]],
    first_index: int = 0,
    concurrency: int = KNOWLEDGE_ORGANIZATION_FRAGMENT_CONCURRENCY,
    input_queue_capacity: int = KNOWLEDGE_ORGANIZATION_INPUT_QUEUE_CAPACITY,
    result_queue_capacity: int = KNOWLEDGE_ORGANIZATION_RESULT_QUEUE_CAPACITY,
    reorder_window: int = KNOWLEDGE_ORGANIZATION_REORDER_WINDOW,
) -> KnowledgeOrganizationPipelineStats:
    if expected_count <= 0 or not 0 <= first_index <= expected_count:
        raise ValueError(t("invalid organization pipeline range"))
    if concurrency <= 0 or input_queue_capacity <= 0 or result_queue_capacity <= 0 or reorder_window <= 0:
        raise ValueError(t("invalid organization pipeline capacity"))
    if first_index == expected_count:
        return KnowledgeOrganizationPipelineStats(0, 0, 0, 0)

    input_queue: asyncio.Queue[InputT | None] = asyncio.Queue(maxsize=input_queue_capacity)
    result_queue: asyncio.Queue[ResultT] = asyncio.Queue(maxsize=result_queue_capacity)
    outstanding_slots = asyncio.Semaphore(reorder_window + 1)
    active_tasks = 0
    max_active_tasks = 0
    max_input_queue_size = 0
    max_result_queue_size = 0
    max_reorder_size = 0

    async def produce() -> None:
        nonlocal max_input_queue_size
        produced_count = first_index
        async for item in inputs:
            if _item_index(item) != produced_count or produced_count >= expected_count:
                raise RuntimeError(t("organization input fragment order mismatch"))
            await outstanding_slots.acquire()
            await input_queue.put(item)
            produced_count += 1
            max_input_queue_size = max(max_input_queue_size, input_queue.qsize())
        if produced_count != expected_count:
            raise RuntimeError(t("organization input fragment count mismatch"))
        for _ in range(concurrency):
            await input_queue.put(None)

    async def work() -> None:
        nonlocal active_tasks, max_active_tasks, max_result_queue_size
        while True:
            item = await input_queue.get()
            try:
                if item is None:
                    return
                active_tasks += 1
                max_active_tasks = max(max_active_tasks, active_tasks)
                try:
                    result = await process(item)
                finally:
                    active_tasks -= 1
                if _item_index(result) != _item_index(item):
                    raise RuntimeError(t("organization result fragment index mismatch"))
                await result_queue.put(result)
                max_result_queue_size = max(max_result_queue_size, result_queue.qsize())
            finally:
                input_queue.task_done()

    async def persist_in_order() -> None:
        nonlocal max_reorder_size
        next_index = first_index
        reorder_buffer: dict[int, ResultT] = {}
        while next_index < expected_count:
            result = await result_queue.get()
            try:
                result_index = _item_index(result)
                if result_index < next_index or result_index in reorder_buffer:
                    raise RuntimeError(t("organization result fragment duplicated"))
                reorder_buffer[result_index] = result
                max_reorder_size = max(max_reorder_size, sum(index > next_index for index in reorder_buffer))
                while next_index in reorder_buffer:
                    ordered = reorder_buffer.pop(next_index)
                    await persist(ordered)
                    outstanding_slots.release()
                    next_index += 1
            finally:
                result_queue.task_done()
        if reorder_buffer:
            raise RuntimeError(t("organization reorder buffer not empty"))

    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(produce())
        for _ in range(concurrency):
            task_group.create_task(work())
        task_group.create_task(persist_in_order())

    return KnowledgeOrganizationPipelineStats(
        max_active_tasks=max_active_tasks,
        max_input_queue_size=max_input_queue_size,
        max_result_queue_size=max_result_queue_size,
        max_reorder_size=max_reorder_size,
    )


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


async def _default_model_caller(
    model: KnowledgeOrganizationModelConfig,
    *,
    scope: tuple[KnowledgeOrganizationScopeItem, ...],
) -> KnowledgeOrganizationPlan:
    payload = canonical_json_dumps([_scope_payload(item) for item in scope])
    if estimate_tokens(payload) > model.input_budget_tokens:
        raise KnowledgeOrganizationContextExceededError(t("organization scope exceeds model input budget"))
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
        plan = KnowledgeOrganizationPlan.model_validate(json.loads(content))
    except Exception as exc:
        raise ValueError(t("organization model result invalid")) from exc
    return _validate_scope_plan(plan, scope=scope)


async def _default_analysis_caller(model: KnowledgeOrganizationModelConfig, *, content: str) -> str:
    payload = canonical_json_dumps({"content": content})
    prompt_tokens = estimate_tokens(KNOWLEDGE_ORGANIZATION_ANALYSIS_SYSTEM_PROMPT)
    analysis_max_output_tokens = min(model.max_output_tokens, KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS)
    available = model.context_window_tokens - analysis_max_output_tokens - model.safety_margin_tokens - prompt_tokens
    if estimate_tokens(payload) > available:
        raise KnowledgeOrganizationContextExceededError(t("organization analysis scope exceeds model input budget"))
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


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _contains_exception(exc: BaseException, exception_type: type[BaseException]) -> bool:
    if isinstance(exc, exception_type):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_exception(nested, exception_type) for nested in exc.exceptions)
    cause = exc.__cause__
    if cause is not None and _contains_exception(cause, exception_type):
        return True
    context = exc.__context__
    return context is not None and not exc.__suppress_context__ and _contains_exception(context, exception_type)


def _model_key(model: KnowledgeOrganizationModelConfig) -> str:
    execution_model = {
        "channel_id": model.channel_id,
        "model_id": model.model_id,
        "protocol": model.protocol,
    }
    return hashlib.sha256(canonical_json_dumps(execution_model).encode("utf-8")).hexdigest()


def _work_key(snapshot: KnowledgeOrganizationSnapshot) -> str:
    payload = {
        "snapshot_key": snapshot.snapshot_key,
        "scope": "knowledge_organization",
        "version": 2,
    }
    return hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()


def _stage_key(
    *,
    snapshot_key: str,
    work_key: str,
    stage_index: int,
    lower_stage_key: str | None,
    model_key: str,
    expected_fragment_count: int,
    purpose: str,
) -> str:
    payload = {
        "snapshot_key": snapshot_key,
        "work_key": work_key,
        "stage_index": stage_index,
        "lower_stage_key": lower_stage_key,
        "model_key": model_key,
        "expected_fragment_count": expected_fragment_count,
        "purpose": purpose,
    }
    return hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()


def _stage_model_snapshot(model: KnowledgeOrganizationModelConfig, *, purpose: str) -> dict[str, Any]:
    return {
        "execution_model": {
            "channel_id": model.channel_id,
            "model_id": model.model_id,
            "protocol": model.protocol,
        },
        "purpose": purpose,
    }


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
    loaded = await _maybe_await(loader(candidates, collection_name))
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
        oversized = [item for item in page_items if _scope_item_tokens(item) > model.input_budget_tokens]
        regular = tuple(item for item in page_items if _scope_item_tokens(item) <= model.input_budget_tokens)
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
                    input_tokens=_scope_item_tokens(item),
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


async def _write_plan_fragment(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
    result: KnowledgeOrganizationFragmentResult,
) -> None:
    fragment = KnowledgeOrganizationFragment(
        dedupe_key=knowledge_organization_fragment_crud.build_dedupe_key(
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            model_key=stage.model_key,
            fragment_index=result.fragment_index,
        ),
        uid=stage.uid,
        knowledge_base_id=stage.knowledge_base_id,
        snapshot_id=stage.snapshot_id,
        stage_id=stage.id,
        work_key=stage.work_key,
        snapshot_key=stage.snapshot_key,
        stage_key=stage.stage_key,
        model_key=stage.model_key,
        fragment_index=result.fragment_index,
        candidate_scope={
            "items": [_scope_descriptor(item) for item in result.scope],
            "output_items": [_compact_scope_descriptor(item) for item in result.output_scope],
            "input_tokens": result.input_tokens,
        },
        result=result.plan.model_dump(mode="json"),
        status=KnowledgeOrganizationFragmentStatus.COMPLETED,
    )
    async with session_factory() as db:
        persisted, _created = await knowledge_organization_fragment_crud.write_ordered(db, fragment=fragment)
    if persisted is None or persisted.candidate_scope != fragment.candidate_scope or persisted.result != fragment.result:
        raise RuntimeError(t("organization fragment persistence conflict"))


async def _stage_resume_index(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> int:
    async with session_factory() as db:
        value = await knowledge_organization_stage_crud.get_resume_fragment_index(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            snapshot_key=stage.snapshot_key,
            model_key=stage.model_key,
        )
    if value is None:
        raise RuntimeError(t("organization stage cannot resume"))
    return value


async def _mark_stage_completed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> None:
    async with session_factory() as db:
        completed = await knowledge_organization_stage_crud.mark_completed(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            snapshot_key=stage.snapshot_key,
            model_key=stage.model_key,
        )
    if not completed:
        async with session_factory() as db:
            current = await knowledge_organization_stage_crud.get_by_identity(db, work_key=stage.work_key, stage_key=stage.stage_key)
        if current is None or current.status != KnowledgeOrganizationStageStatus.COMPLETED:
            raise RuntimeError(t("organization stage completion barrier rejected"))


async def _fail_and_invalidate_stage(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
    error: str,
) -> None:
    async with session_factory() as db:
        await knowledge_organization_stage_crud.mark_failed(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            snapshot_key=stage.snapshot_key,
            model_key=stage.model_key,
            error=error,
        )
    async with session_factory() as db:
        await knowledge_organization_stage_crud.invalidate(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            snapshot_key=stage.snapshot_key,
            model_key=stage.model_key,
        )


async def _create_stage(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    work_key: str,
    stage_index: int,
    lower_stage_key: str | None,
    model: KnowledgeOrganizationModelConfig,
    expected_fragment_count: int,
    purpose: str,
) -> KnowledgeOrganizationStage:
    model_key = _model_key(model)
    stage_key = _stage_key(
        snapshot_key=snapshot.snapshot_key,
        work_key=work_key,
        stage_index=stage_index,
        lower_stage_key=lower_stage_key,
        model_key=model_key,
        expected_fragment_count=expected_fragment_count,
        purpose=purpose,
    )
    stage = KnowledgeOrganizationStage(
        uid=snapshot.uid,
        knowledge_base_id=snapshot.knowledge_base_id,
        snapshot_id=snapshot.id,
        work_key=work_key,
        snapshot_key=snapshot.snapshot_key,
        stage_key=stage_key,
        stage_index=stage_index,
        lower_stage_key=lower_stage_key,
        model_key=model_key,
        model_snapshot=_stage_model_snapshot(model, purpose=purpose),
        expected_fragment_count=expected_fragment_count,
    )
    async with session_factory() as db:
        persisted, _created = await knowledge_organization_stage_crud.create_stage(db, stage=stage)
    return persisted


async def _iter_stage_fragments(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> AsyncIterator[KnowledgeOrganizationFragment]:
    after_index = -1
    expected_index = 0
    while True:
        async with session_factory() as db:
            page = await knowledge_organization_fragment_crud.list_stage_page(
                db,
                stage_id=stage.id,
                after_fragment_index=after_index,
                limit=KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE,
            )
        if not page:
            break
        for fragment in page:
            if fragment.fragment_index != expected_index or fragment.status != KnowledgeOrganizationFragmentStatus.COMPLETED:
                raise RuntimeError(t("organization stage fragment sequence invalid"))
            yield fragment
            expected_index += 1
            after_index = fragment.fragment_index
        if len(page) < KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE:
            break
    if expected_index != stage.expected_fragment_count:
        raise RuntimeError(t("organization completed stage fragment count invalid"))


async def _read_analysis_stage_text(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    stage: KnowledgeOrganizationStage,
) -> str:
    summaries: list[str] = []
    async for fragment in _iter_stage_fragments(session_factory, stage=stage):
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
    max_part_tokens = model.context_window_tokens - analysis_max_output_tokens - model.safety_margin_tokens - analysis_prompt_tokens - 32
    if max_part_tokens <= 0:
        raise KnowledgeOrganizationContextExceededError(t("organization model cannot fit minimal analysis input"))
    parts = split_content_for_analysis(content, max_tokens=max_part_tokens)
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
    stage = await _create_stage(
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
        raise KnowledgeOrganizationModelFailedError(t("organization analysis stage already invalidated"))
    if stage.status == KnowledgeOrganizationStageStatus.COMPLETED:
        reduced = await _read_analysis_stage_text(session_factory, stage=stage)
        if estimate_tokens(reduced) >= estimate_tokens(content):
            exc = KnowledgeOrganizationNotConvergedError("organization long-item analysis did not reduce")
            await _fail_and_invalidate_stage(session_factory, stage=stage, error=f"{type(exc).__name__}: {exc}")
            raise exc
        return reduced, stage

    last_error: BaseException | None = None
    for attempt in range(KNOWLEDGE_ORGANIZATION_MODEL_MAX_ATTEMPTS):
        first_index = await _stage_resume_index(session_factory, stage=stage)

        async def inputs() -> AsyncIterator[tuple[int, str]]:
            for index in range(first_index, expected_count):
                yield index, parts[index]

        async def process(value: tuple[int, str]) -> tuple[int, str, str]:
            index, part = value
            summary = await _maybe_await(analysis_caller(model, content=part))
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
                await run_bounded_knowledge_organization_pipeline(
                    inputs=inputs(),
                    expected_count=expected_count,
                    process=process,
                    persist=persist,
                    first_index=first_index,
                )
            reduced = await _read_analysis_stage_text(session_factory, stage=stage)
            if estimate_tokens(reduced) >= estimate_tokens(content):
                raise KnowledgeOrganizationNotConvergedError(t("organization long-item analysis did not reduce"))
            await _mark_stage_completed(session_factory, stage=stage)
            return reduced, stage
        except KnowledgeOrganizationNotConvergedError as exc:
            await _fail_and_invalidate_stage(session_factory, stage=stage, error=f"{type(exc).__name__}: {exc}")
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 >= KNOWLEDGE_ORGANIZATION_MODEL_MAX_ATTEMPTS:
                await _fail_and_invalidate_stage(session_factory, stage=stage, error=f"{type(exc).__name__}: {exc}")
                raise
    raise KnowledgeOrganizationModelFailedError(str(last_error) if last_error is not None else t("organization analysis failed"))


async def _compact_scope_item(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    work_key: str,
    model: KnowledgeOrganizationModelConfig,
    item: KnowledgeOrganizationScopeItem,
    analysis_caller: Callable[..., Any],
) -> tuple[KnowledgeOrganizationScopeItem, int]:
    if _scope_item_tokens(item) <= model.input_budget_tokens:
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
    if _scope_item_tokens(minimum_item) > model.input_budget_tokens:
        raise KnowledgeOrganizationContextExceededError(t("organization model cannot fit minimum knowledge metadata"))
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
        if _scope_item_tokens(compacted) <= model.input_budget_tokens:
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
    if _scope_tokens(result) > model.input_budget_tokens:
        raise KnowledgeOrganizationContextExceededError(t("organization grouped scope exceeds model input budget"))
    return result, analysis_stage_count


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

        if len(touched) == 1:
            if ref_set != set(touched[0].sources):
                raise ValueError(t("organization reduction candidate coverage mismatch"))
            effective_item = touched[0].effective_item or plan_item
        else:
            if not isinstance(plan_item, (KnowledgeOrganizationMerge, KnowledgeOrganizationConflict)):
                raise ValueError(t("organization reduction cross-candidate action invalid"))
            if isinstance(plan_item, KnowledgeOrganizationMerge) and any(isinstance(candidate.effective_item, KnowledgeOrganizationConflict) for candidate in touched):
                raise ValueError(t("organization reduction cannot merge an unresolved conflict"))
            effective_item = plan_item

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
        raise KnowledgeOrganizationNotConvergedError(t(f"organization reduction did not strictly decrease: input={lower_input_tokens}, output={output_tokens}"))


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
) -> tuple[_StageExecutionResult, int]:
    if single_scope is not None:
        expected_count = 1
    elif lower_stage is None:
        expected_count = await _count_groups(
            _iter_initial_groups(
                session_factory,
                snapshot=snapshot,
                model=model,
                collection_name=collection_name,
                semantic_neighbor_loader=semantic_neighbor_loader,
            )
        )
    else:
        expected_count = await _count_groups(_iter_reduction_groups(session_factory, lower_stage=lower_stage, model=model))
    if expected_count <= 0:
        raise KnowledgeOrganizationModelFailedError(t("organization stage has no candidate groups"))

    purpose = "initial" if lower_stage is None else "reduction"
    stage = await _create_stage(
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
        raise KnowledgeOrganizationModelFailedError(t("organization stage already invalidated"))
    if stage.status == KnowledgeOrganizationStageStatus.COMPLETED:
        output_tokens = await _measure_stage_output_tokens(session_factory, stage=stage)
        try:
            await _validate_reduction_decrease(
                session_factory,
                lower_stage=lower_stage,
                output_tokens=output_tokens,
            )
        except KnowledgeOrganizationNotConvergedError as exc:
            await _fail_and_invalidate_stage(session_factory, stage=stage, error=f"{type(exc).__name__}: {exc}")
            raise
        return _StageExecutionResult(stage=stage, input_tokens=0, output_tokens=output_tokens), 0

    last_error: BaseException | None = None
    analysis_stage_count = 0
    last_stats: KnowledgeOrganizationPipelineStats | None = None
    for attempt in range(KNOWLEDGE_ORGANIZATION_MODEL_MAX_ATTEMPTS):
        first_index = await _stage_resume_index(session_factory, stage=stage)

        async def inputs() -> AsyncIterator[KnowledgeOrganizationFragmentInput]:
            if single_scope is not None:
                if first_index == 0:
                    yield KnowledgeOrganizationFragmentInput(fragment_index=0, scope=single_scope, input_tokens=_scope_tokens(single_scope))
                return
            if lower_stage is None:
                async for item in _iter_initial_groups(
                    session_factory,
                    snapshot=snapshot,
                    model=model,
                    collection_name=collection_name,
                    semantic_neighbor_loader=semantic_neighbor_loader,
                    first_index=first_index,
                ):
                    yield item
                return
            async for item in _iter_reduction_groups(
                session_factory,
                lower_stage=lower_stage,
                model=model,
                first_index=first_index,
            ):
                yield item

        input_token_total = 0

        async def process(fragment: KnowledgeOrganizationFragmentInput) -> KnowledgeOrganizationFragmentResult:
            nonlocal input_token_total, analysis_stage_count
            prepared_scope, added_analysis_stages = await _prepare_scope_for_model(
                session_factory,
                snapshot=snapshot,
                work_key=work_key,
                model=model,
                scope=fragment.scope,
                analysis_caller=analysis_caller,
            )
            analysis_stage_count += added_analysis_stages
            input_tokens = _scope_tokens(prepared_scope)
            input_token_total += input_tokens
            plan = await _maybe_await(model_caller(model, scope=prepared_scope))
            if not isinstance(plan, KnowledgeOrganizationPlan):
                plan = KnowledgeOrganizationPlan.model_validate(plan)
            plan = _validate_scope_plan(plan, scope=prepared_scope)
            plan, output_scope = _compose_scope_plan(plan, scope=prepared_scope)
            return KnowledgeOrganizationFragmentResult(
                fragment_index=fragment.fragment_index,
                scope=prepared_scope,
                plan=plan,
                output_scope=output_scope,
                input_tokens=input_tokens,
                output_tokens=_plan_tokens(plan),
            )

        try:
            if first_index < expected_count:
                last_stats = await run_bounded_knowledge_organization_pipeline(
                    inputs=inputs(),
                    expected_count=expected_count,
                    process=process,
                    persist=lambda result: _write_plan_fragment(session_factory, stage=stage, result=result),
                    first_index=first_index,
                )
            output_tokens = await _measure_stage_output_tokens(session_factory, stage=stage)
            await _validate_reduction_decrease(
                session_factory,
                lower_stage=lower_stage,
                output_tokens=output_tokens,
            )
            await _mark_stage_completed(session_factory, stage=stage)
            return _StageExecutionResult(stage=stage, input_tokens=input_token_total, output_tokens=output_tokens, stats=last_stats), analysis_stage_count
        except KnowledgeOrganizationNotConvergedError as exc:
            await _fail_and_invalidate_stage(session_factory, stage=stage, error=f"{type(exc).__name__}: {exc}")
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 >= KNOWLEDGE_ORGANIZATION_MODEL_MAX_ATTEMPTS:
                await _fail_and_invalidate_stage(session_factory, stage=stage, error=f"{type(exc).__name__}: {exc}")
                raise
    raise KnowledgeOrganizationModelFailedError(str(last_error) if last_error is not None else t("organization stage failed"))


async def execute_knowledge_organization(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    knowledge_base_id: int,
    model_candidates: tuple[KnowledgeOrganizationModelConfig, ...] | None = None,
    model_caller: Callable[..., Any] | None = None,
    analysis_caller: Callable[..., Any] | None = None,
    semantic_neighbor_loader: Callable[..., Any] | None = None,
) -> KnowledgeOrganizationExecutionResult:
    async def resolve_models() -> tuple[KnowledgeOrganizationModelConfig, ...]:
        if model_candidates is not None:
            resolved = tuple(model_candidates)
        else:
            async with session_factory() as db:
                try:
                    resolved = await load_knowledge_organization_model_candidates(db, uid=uid)
                except Exception as exc:
                    logger.bind(uid=uid, knowledge_base_id=knowledge_base_id).warning(t(LOG_KNOWLEDGE_ORGANIZATION_CONFIG_INVALID, error=str(exc)))
                    raise KnowledgeOrganizationModelFailedError(t("organization model configuration unavailable")) from exc
        if not resolved:
            raise KnowledgeOrganizationModelFailedError(t("organization has no model candidates"))
        return resolved

    models = await resolve_models()
    call_model = model_caller or _default_model_caller
    call_analysis = analysis_caller or _default_analysis_caller
    load_neighbors = semantic_neighbor_loader or (lambda scope, collection: load_vector_semantic_neighbors(scope, collection_name=collection))

    async with session_factory() as db:
        snapshot = await create_knowledge_organization_snapshot(db, uid=uid, knowledge_base_id=knowledge_base_id)
        knowledge_base = await knowledge_base_crud.get(db, knowledge_base_id)
        if knowledge_base is None or knowledge_base.uid != uid:
            raise KnowledgeOrganizationModelFailedError(t("organization knowledge base missing"))
        collection_name = resolve_active_knowledge_base_embedding(knowledge_base).collection_name
    if snapshot.item_count == 0:
        return KnowledgeOrganizationExecutionResult(
            plan=KnowledgeOrganizationPlan(items=()),
            model_id=None,
            stage_count=0,
            snapshot_id=snapshot.id,
        )

    work_key = _work_key(snapshot)
    lower_stage: KnowledgeOrganizationStage | None = None
    stage_count = 0
    layer_index = 0
    while True:
        if model_candidates is None and layer_index > 0:
            models = await resolve_models()
        selected_result: _StageExecutionResult | None = None
        selected_model: KnowledgeOrganizationModelConfig | None = None
        single_scope: tuple[KnowledgeOrganizationScopeItem, ...] | None = None
        if lower_stage is None:
            single_scope = await _try_load_single_request_scope(
                session_factory,
                snapshot=snapshot,
                model=models[0],
            )

        layer_errors: list[BaseException] = []
        for model in models:
            model_single_scope = single_scope
            if lower_stage is None and model is not models[0] and single_scope is None:
                model_single_scope = await _try_load_single_request_scope(session_factory, snapshot=snapshot, model=model)
            try:
                selected_result, added_analysis_stages = await _execute_plan_stage_for_model(
                    session_factory,
                    snapshot=snapshot,
                    work_key=work_key,
                    stage_index=layer_index,
                    lower_stage=lower_stage,
                    model=model,
                    collection_name=collection_name,
                    model_caller=call_model,
                    analysis_caller=call_analysis,
                    semantic_neighbor_loader=load_neighbors,
                    single_scope=model_single_scope,
                )
                stage_count += 1 + added_analysis_stages
                selected_model = model
                break
            except Exception as exc:
                layer_errors.append(exc)
                continue
        if selected_result is None or selected_model is None:
            if layer_errors and all(_contains_exception(error, KnowledgeOrganizationContextExceededError) for error in layer_errors):
                raise KnowledgeOrganizationContextExceededError(t("all organization models cannot fit minimal input")) from layer_errors[-1]
            if layer_errors and all(_contains_exception(error, KnowledgeOrganizationNotConvergedError) for error in layer_errors):
                raise KnowledgeOrganizationNotConvergedError(t("all organization models failed to converge")) from layer_errors[-1]
            raise KnowledgeOrganizationModelFailedError(t("all organization models failed")) from (layer_errors[-1] if layer_errors else None)

        completed_stage = selected_result.stage
        if completed_stage.expected_fragment_count == 1:
            return KnowledgeOrganizationExecutionResult(
                plan=await _combine_stage_plan(session_factory, stage=completed_stage),
                model_id=selected_model.model_id,
                stage_count=stage_count,
                snapshot_id=snapshot.id,
            )
        lower_stage = completed_stage
        layer_index += 1


__all__ = [
    "KnowledgeOrganizationContextExceededError",
    "KnowledgeOrganizationExecutionError",
    "KnowledgeOrganizationExecutionResult",
    "KnowledgeOrganizationModelFailedError",
    "KnowledgeOrganizationNotConvergedError",
    "KnowledgeOrganizationPipelineStats",
    "KnowledgeOrganizationScopeItem",
    "execute_knowledge_organization",
    "run_bounded_knowledge_organization_pipeline",
]
