from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_EMBEDDING_VECTOR_EMPTY,
    ERR_KB_DENSE_RETRIEVAL_FAILED,
    KNOWLEDGE_RECALL_MAX_CONCURRENCY,
    LOG_KNOWLEDGE_RECALL_CANDIDATE_WINDOW_EXPANDED,
    LOG_KNOWLEDGE_RECALL_SOURCE_FAILED,
)
from app.core.crud.knowledge.base import knowledge_base_crud, knowledge_base_document_crud
from app.core.embedding.common import build_embedding_signature
from app.core.embedding.knowledge_base import embed_chunks_with_knowledge_base_config
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.knowledge.recall import filter_recallable_managed_hits, materialize_recallable_managed_hits
from app.core.knowledge.results import KnowledgeRecallItem, KnowledgeRecallResult, KnowledgeRecallSourceType
from app.core.log import get_logger
from app.core.rerank.knowledge_base import get_profile_rerank_config, rerank_retrieval_hits
from app.core.retrieval.hybrid import hybrid_query_collection
from app.core.retrieval.schemas import RetrievalHit
from app.core.utils.text_splitter import TextSplitter
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseDocument, KnowledgeBaseIndexStatus, KnowledgeBaseType
from app.models.profile import Profile, ProfileConfig

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _RecallSource:
    knowledge_base: KnowledgeBase
    collection_name: str
    embedding_group: tuple[str, int, str, int | None]
    order: int


@dataclass(slots=True)
class _SourceState:
    source: _RecallSource
    query_embedding: list[float]
    requested: int
    target_count: int
    previous_raw_count: int = -1
    hits: list[RetrievalHit] | None = None
    done: bool = False
    failed: bool = False
    documents: dict[tuple[str, object], KnowledgeBaseDocument] = field(default_factory=dict)
    document_keys_checked: set[tuple[str, object]] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Candidate:
    source: _RecallSource
    hit: RetrievalHit
    local_rank: int


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _is_managed_knowledge_base(knowledge_base: KnowledgeBase) -> bool:
    value = getattr(knowledge_base.knowledge_base_type, "value", knowledge_base.knowledge_base_type)
    return value == KnowledgeBaseType.LLM_MANAGED.value


def _build_recall_source(knowledge_base: KnowledgeBase, order: int) -> _RecallSource | None:
    status = getattr(knowledge_base.index_status, "value", knowledge_base.index_status)
    if status != KnowledgeBaseIndexStatus.READY.value:
        return None
    try:
        embedding = resolve_active_knowledge_base_embedding(knowledge_base)
    except Exception:
        return None
    if isinstance(embedding.channel_id, bool) or not isinstance(embedding.channel_id, int) or embedding.channel_id <= 0:
        return None
    if not isinstance(embedding.model_id, str) or not embedding.model_id.strip():
        return None
    if not isinstance(embedding.collection_name, str) or not embedding.collection_name.strip():
        return None
    if embedding.dimensions is not None and (isinstance(embedding.dimensions, bool) or not isinstance(embedding.dimensions, int) or embedding.dimensions <= 0):
        return None
    signature = getattr(knowledge_base, "active_embedding_signature", None)
    if not isinstance(signature, str) or not signature.strip():
        if embedding.dimensions is None:
            signature = str((embedding.channel_id, embedding.model_id, embedding.dimensions))
        else:
            signature = build_embedding_signature(embedding.channel_id, embedding.model_id, embedding.dimensions)
    return _RecallSource(
        knowledge_base=knowledge_base,
        collection_name=embedding.collection_name,
        embedding_group=(signature, embedding.channel_id, embedding.model_id, embedding.dimensions),
        order=order,
    )


def _document_chunk_key(hit: RetrievalHit) -> tuple[str, object] | None:
    metadata = hit.metadata or {}
    if metadata.get("knowledge_type") == "managed":
        return None
    document_id = _positive_int(metadata.get("document_id"))
    if document_id is not None:
        return ("document_id", document_id)
    document_uuid = metadata.get("document_uuid")
    if isinstance(document_uuid, str) and document_uuid.strip():
        return ("document_uuid", document_uuid)
    return None


def _document_recall_keys(document: KnowledgeBaseDocument) -> tuple[tuple[str, object], ...]:
    keys: list[tuple[str, object]] = []
    document_id = _positive_int(document.id)
    if document_id is not None:
        keys.append(("document_id", document_id))
    document_uuid = (document.metadata_ or {}).get("document_uuid")
    if isinstance(document_uuid, str) and document_uuid.strip():
        keys.append(("document_uuid", document_uuid))
    return tuple(keys)


async def _load_user_recall_documents(
    db: AsyncSession,
    *,
    knowledge_base_id: int,
    hits: list[RetrievalHit],
    documents: dict[tuple[str, object], KnowledgeBaseDocument],
    checked_keys: set[tuple[str, object]],
) -> None:
    chunk_indexes_by_key: dict[tuple[str, object], set[int]] = {}
    for hit in hits:
        key = _document_chunk_key(hit)
        chunk_index = _nonnegative_int((hit.metadata or {}).get("chunk_index"))
        if key is None or chunk_index is None:
            continue
        chunk_indexes_by_key.setdefault(key, set()).add(chunk_index)

    requested_keys = {
        key
        for key, chunk_indexes in chunk_indexes_by_key.items()
        if any(chunk_index + 1 in chunk_indexes for chunk_index in chunk_indexes)
    }
    missing_keys = requested_keys - checked_keys
    if not missing_keys:
        return

    document_ids = [value for kind, value in missing_keys if kind == "document_id" and isinstance(value, int)]
    document_uuids = [value for kind, value in missing_keys if kind == "document_uuid" and isinstance(value, str)]
    loaded_documents = await knowledge_base_document_crud.list_by_recall_references(
        db,
        knowledge_base_id=knowledge_base_id,
        document_ids=document_ids,
        document_uuids=document_uuids,
    )
    checked_keys.update(missing_keys)
    for document in loaded_documents:
        for key in _document_recall_keys(document):
            documents[key] = document


def _merge_adjacent_user_document_hits(
    hits: list[RetrievalHit],
    *,
    documents: dict[tuple[str, object], KnowledgeBaseDocument],
) -> list[RetrievalHit]:
    grouped: dict[tuple[str, object], list[tuple[int, int, RetrievalHit]]] = {}
    passthrough: list[tuple[int, RetrievalHit]] = []
    for position, hit in enumerate(hits):
        key = _document_chunk_key(hit)
        chunk_index = _nonnegative_int((hit.metadata or {}).get("chunk_index"))
        if key is None or chunk_index is None:
            passthrough.append((position, hit))
            continue
        grouped.setdefault(key, []).append((position, chunk_index, hit))

    merged_with_positions = list(passthrough)
    for key, entries in grouped.items():
        entries.sort(key=lambda item: (item[1], item[0]))
        document = documents.get(key)
        run_start = 0
        while run_start < len(entries):
            run_end = run_start
            while run_end + 1 < len(entries) and entries[run_end + 1][1] == entries[run_end][1] + 1:
                run_end += 1

            run = entries[run_start : run_end + 1]
            if len(run) == 1 or document is None:
                for position, chunk_index, hit in run:
                    metadata = dict(hit.metadata or {})
                    metadata.update({"chunk_start_index": chunk_index, "chunk_end_index": chunk_index})
                    merged_with_positions.append((position, replace(hit, metadata=metadata)))
                run_start = run_end + 1
                continue

            _, start_chunk_index, first_hit = run[0]
            end_chunk_index = run[-1][1]
            content = TextSplitter(
                chunk_size=document.chunk_size,
                chunk_overlap=document.chunk_overlap,
            ).reconstruct_chunk_range(
                document.content,
                start_chunk_index,
                end_chunk_index,
            )
            if content is None:
                for position, chunk_index, hit in run:
                    metadata = dict(hit.metadata or {})
                    metadata.update({"chunk_start_index": chunk_index, "chunk_end_index": chunk_index})
                    merged_with_positions.append((position, replace(hit, metadata=metadata)))
                run_start = run_end + 1
                continue

            metadata = dict(first_hit.metadata or {})
            metadata.update({"chunk_start_index": start_chunk_index, "chunk_end_index": end_chunk_index})
            merged_with_positions.append((min(position for position, _, _ in run), replace(first_hit, content=content, metadata=metadata)))
            run_start = run_end + 1

    merged_with_positions.sort(key=lambda item: item[0])
    return [hit for _, hit in merged_with_positions]


def _log_source_failure(source: _RecallSource, error: Exception, *, stage: str) -> None:
    knowledge_base = source.knowledge_base
    logger.bind(
        knowledge_base_id=knowledge_base.id,
        knowledge_base_name=knowledge_base.name,
        collection_name=source.collection_name,
        recall_stage=stage,
    ).warning(t(LOG_KNOWLEDGE_RECALL_SOURCE_FAILED, error=str(error)))


async def _query_source_vectors(
    source: _RecallSource,
    query_embedding: list[float],
    query: str,
    limit: int,
    semaphore: asyncio.Semaphore,
) -> list[RetrievalHit]:
    async with semaphore:
        return await hybrid_query_collection(
            source.collection_name,
            query_embedding,
            query,
            limit,
            error_key=ERR_KB_DENSE_RETRIEVAL_FAILED,
        )


async def _finalize_source_hits(
    db: AsyncSession,
    *,
    state: _SourceState,
    hits: list[RetrievalHit],
) -> None:
    knowledge_base = state.source.knowledge_base
    if not _is_managed_knowledge_base(knowledge_base):
        knowledge_base_id = _positive_int(knowledge_base.id)
        if knowledge_base_id is not None:
            try:
                await _load_user_recall_documents(
                    db,
                    knowledge_base_id=knowledge_base_id,
                    hits=hits,
                    documents=state.documents,
                    checked_keys=state.document_keys_checked,
                )
            except Exception as exc:
                _log_source_failure(state.source, exc, stage="document_resolve")
    state.hits = _merge_adjacent_user_document_hits(hits, documents=state.documents)
    state.done = True


async def _recall_source_candidates(
    db: AsyncSession,
    *,
    states: list[_SourceState],
    profile_uid: str,
    query: str,
) -> None:
    semaphore = asyncio.Semaphore(KNOWLEDGE_RECALL_MAX_CONCURRENCY)
    while True:
        pending = [state for state in states if not state.done and not state.failed]
        if not pending:
            return

        await db.commit()
        raw_results = await asyncio.gather(
            *[
                _query_source_vectors(
                    state.source,
                    state.query_embedding,
                    query,
                    state.requested,
                    semaphore,
                )
                for state in pending
            ],
            return_exceptions=True,
        )

        for state, raw_result in zip(pending, raw_results, strict=True):
            if isinstance(raw_result, BaseException):
                state.failed = True
                error = raw_result if isinstance(raw_result, Exception) else RuntimeError(str(raw_result))
                _log_source_failure(state.source, error, stage="hybrid")
                continue

            try:
                knowledge_base = state.source.knowledge_base
                filtered_hits = await filter_recallable_managed_hits(
                    db,
                    uid=profile_uid,
                    knowledge_base_id=knowledge_base.id,
                    hits=raw_result,
                )
            except Exception as exc:
                state.failed = True
                _log_source_failure(state.source, exc, stage="filter")
                continue

            valid_count = len(filtered_hits)
            if valid_count >= state.target_count:
                await _finalize_source_hits(
                    db,
                    state=state,
                    hits=filtered_hits[: state.target_count],
                )
                continue

            raw_count = len(raw_result)
            if raw_count < state.requested or raw_count <= state.previous_raw_count:
                await _finalize_source_hits(
                    db,
                    state=state,
                    hits=filtered_hits,
                )
                continue

            previous_limit = state.requested
            state.previous_raw_count = raw_count
            state.requested *= 2
            logger.bind(
                knowledge_base_id=knowledge_base.id,
                previous_limit=previous_limit,
                next_limit=state.requested,
                valid_count=valid_count,
                target_count=state.target_count,
            ).info(
                t(
                    LOG_KNOWLEDGE_RECALL_CANDIDATE_WINDOW_EXPANDED,
                    previous_limit=previous_limit,
                    next_limit=state.requested,
                    valid_count=valid_count,
                    target_count=state.target_count,
                )
            )


def _build_global_fallback_candidates(states: list[_SourceState], candidate_k: int) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for state in states:
        if state.failed or not state.hits:
            continue
        for local_rank, hit in enumerate(state.hits, start=1):
            candidates.append(_Candidate(source=state.source, hit=hit, local_rank=local_rank))

    candidates.sort(key=lambda candidate: (candidate.local_rank, candidate.source.order))
    return candidates[:candidate_k]


async def _global_rerank(
    db: AsyncSession,
    *,
    profile: Profile,
    query: str,
    fallback_candidates: list[_Candidate],
) -> list[_Candidate]:
    if len(fallback_candidates) <= 1:
        return fallback_candidates

    excluded_priorities: set[int] = set()
    while True:
        try:
            rerank_config = await get_profile_rerank_config(
                db,
                profile,
                excluded_priorities=excluded_priorities,
            )
        except LLMException as exc:
            logger.bind(profile_id=profile.id).warning(
                t("LOG_RERANK_CONFIG_READ_FAILED", error=t(exc.message, default=exc.message, **exc.kwargs))
            )
            return fallback_candidates

        if rerank_config is None:
            return fallback_candidates

        try:
            await db.commit()
            started = time.perf_counter()
            reranked_hits = await rerank_retrieval_hits(
                rerank_config,
                query,
                [candidate.hit for candidate in fallback_candidates],
                len(fallback_candidates),
            )
            latency_ms = (time.perf_counter() - started) * 1000
        except LLMException as exc:
            excluded_priorities.add(rerank_config.priority)
            logger.bind(
                profile_id=profile.id,
                rerank_channel_id=getattr(rerank_config, "channel_id", None),
                rerank_channel_name=getattr(rerank_config, "channel_name", None),
                rerank_model_id=rerank_config.model_id,
            ).warning(t("LOG_RERANK_REMOTE_CALL_FAILED", error=t(exc.message, default=exc.message, **exc.kwargs)))
            continue

        candidates_by_hit = {id(candidate.hit): candidate for candidate in fallback_candidates}
        reranked_candidates: list[_Candidate] = []
        seen: set[int] = set()
        for hit in reranked_hits:
            candidate = candidates_by_hit.get(id(hit))
            if candidate is None or id(candidate.hit) in seen:
                continue
            seen.add(id(candidate.hit))
            reranked_candidates.append(candidate)
        for candidate in fallback_candidates:
            if id(candidate.hit) not in seen:
                reranked_candidates.append(candidate)

        logger.bind(
            profile_id=profile.id,
            candidate_count=len(fallback_candidates),
            rerank_model_id=rerank_config.model_id,
            rerank_latency_ms=round(latency_ms, 2),
        ).info(t("LOG_RERANK_REMOTE_FINISHED"))
        return reranked_candidates


async def _materialize_managed_candidates(
    db: AsyncSession,
    *,
    profile_uid: str,
    candidates: list[_Candidate],
) -> list[_Candidate]:
    managed_groups: dict[int, list[_Candidate]] = {}
    for candidate in candidates:
        knowledge_base = candidate.source.knowledge_base
        if not _is_managed_knowledge_base(knowledge_base):
            continue
        knowledge_base_id = _positive_int(knowledge_base.id)
        if knowledge_base_id is not None:
            managed_groups.setdefault(knowledge_base_id, []).append(candidate)

    materialized_by_key: dict[tuple[int, str], RetrievalHit] = {}
    failed_knowledge_bases: set[int] = set()
    for knowledge_base_id, group in managed_groups.items():
        source = group[0].source
        try:
            materialized_hits = await materialize_recallable_managed_hits(
                db,
                uid=profile_uid,
                knowledge_base_id=knowledge_base_id,
                hits=[candidate.hit for candidate in group],
            )
        except Exception as exc:
            failed_knowledge_bases.add(knowledge_base_id)
            _log_source_failure(source, exc, stage="materialize")
            continue
        for hit in materialized_hits:
            materialized_by_key[(knowledge_base_id, hit.id)] = hit

    materialized_candidates: list[_Candidate] = []
    for candidate in candidates:
        knowledge_base = candidate.source.knowledge_base
        if not _is_managed_knowledge_base(knowledge_base):
            materialized_candidates.append(candidate)
            continue
        knowledge_base_id = _positive_int(knowledge_base.id)
        if knowledge_base_id is None or knowledge_base_id in failed_knowledge_bases:
            continue
        materialized_hit = materialized_by_key.get((knowledge_base_id, candidate.hit.id))
        if materialized_hit is None:
            continue
        materialized_candidates.append(replace(candidate, hit=materialized_hit))
    return materialized_candidates


def _build_recall_items(
    ordered_hits: list[tuple[KnowledgeBase, RetrievalHit]],
    *,
    top_k: int,
    result_max_chars: int,
) -> tuple[KnowledgeRecallItem, ...]:
    items: list[KnowledgeRecallItem] = []
    remaining_chars = result_max_chars
    for knowledge_base, hit in ordered_hits:
        if len(items) >= top_k or remaining_chars <= 0:
            break
        content = hit.content or ""
        if not content:
            continue

        truncated = len(content) > remaining_chars
        output_content = content[:remaining_chars] if truncated else content
        metadata = hit.metadata or {}
        knowledge_base_id = _positive_int(knowledge_base.id)
        if knowledge_base_id is None:
            continue

        if _is_managed_knowledge_base(knowledge_base):
            knowledge_id = _positive_int(metadata.get("managed_knowledge_id"))
            expected_version = _positive_int(metadata.get("managed_knowledge_version"))
            raw_key = metadata.get("managed_knowledge_key")
            knowledge_key = raw_key.strip() if isinstance(raw_key, str) and raw_key.strip() else None
            maintainable = (
                metadata.get("managed_knowledge_llm_maintainable") is True
                and knowledge_id is not None
                and expected_version is not None
                and knowledge_key is not None
                and not truncated
            )
            if truncated:
                knowledge_id = None
                expected_version = None
                knowledge_key = None
            item = KnowledgeRecallItem(
                knowledge_base_id=knowledge_base_id,
                knowledge_base_name=knowledge_base.name,
                source_type=KnowledgeRecallSourceType.MANAGED_KNOWLEDGE,
                source=knowledge_key or "managed_knowledge",
                content=output_content,
                truncated=truncated,
                llm_maintainable=maintainable,
                knowledge_id=knowledge_id,
                knowledge_key=knowledge_key,
                knowledge_expected_version=expected_version,
            )
        else:
            filename = metadata.get("filename")
            document_uuid = metadata.get("document_uuid")
            if isinstance(filename, str) and filename.strip():
                source = filename.strip()
            elif isinstance(document_uuid, str) and document_uuid.strip():
                source = document_uuid.strip()
            else:
                source = hit.id
            item = KnowledgeRecallItem(
                knowledge_base_id=knowledge_base_id,
                knowledge_base_name=knowledge_base.name,
                source_type=KnowledgeRecallSourceType.USER_KNOWLEDGE,
                source=source,
                content=output_content,
                truncated=truncated,
                llm_maintainable=False,
                document_id=_positive_int(metadata.get("document_id")),
            )

        items.append(item)
        remaining_chars -= len(output_content)
        if truncated:
            break
    return tuple(items)


class UnifiedKnowledgeRecallService:
    async def recall(
        self,
        db: AsyncSession,
        profile: Profile,
        query: str,
    ) -> KnowledgeRecallResult:
        config = ProfileConfig.model_validate(profile.configs or {}).memory.knowledge
        knowledge_bases = await knowledge_base_crud.list_recall_sources_by_profile(
            db,
            uid=profile.uid,
            profile_id=profile.id,
        )

        sources: list[_RecallSource] = []
        for order, knowledge_base in enumerate(knowledge_bases):
            source = _build_recall_source(knowledge_base, order)
            if source is not None:
                sources.append(source)
        if not sources:
            return KnowledgeRecallResult()

        sources_by_embedding: dict[tuple[str, int, str, int | None], list[_RecallSource]] = {}
        for source in sources:
            sources_by_embedding.setdefault(source.embedding_group, []).append(source)

        query_embeddings: dict[tuple[str, int, str, int | None], list[float]] = {}
        for embedding_group, grouped_sources in sources_by_embedding.items():
            representative = grouped_sources[0]
            try:
                embeddings = await embed_chunks_with_knowledge_base_config(
                    db,
                    representative.knowledge_base,
                    [query],
                    1,
                    release_connection=True,
                )
                if not embeddings or not embeddings[0]:
                    raise ValueError(t(ERR_EMBEDDING_VECTOR_EMPTY))
                query_embeddings[embedding_group] = embeddings[0]
            except Exception as exc:
                for source in grouped_sources:
                    _log_source_failure(source, exc, stage="embedding")

        states = [
            _SourceState(
                source=source,
                query_embedding=query_embeddings[source.embedding_group],
                requested=config.candidate_k,
                target_count=config.candidate_k,
            )
            for source in sources
            if source.embedding_group in query_embeddings
        ]
        if not states:
            return KnowledgeRecallResult()

        await _recall_source_candidates(
            db,
            states=states,
            profile_uid=profile.uid,
            query=query,
        )
        fallback_candidates = _build_global_fallback_candidates(states, config.candidate_k)
        if not fallback_candidates:
            return KnowledgeRecallResult()

        ordered_candidates = await _global_rerank(
            db,
            profile=profile,
            query=query,
            fallback_candidates=fallback_candidates,
        )
        materialized_candidates = await _materialize_managed_candidates(
            db,
            profile_uid=profile.uid,
            candidates=ordered_candidates,
        )
        items = _build_recall_items(
            [(candidate.source.knowledge_base, candidate.hit) for candidate in materialized_candidates],
            top_k=config.top_k,
            result_max_chars=config.result_max_chars,
        )
        return KnowledgeRecallResult(items=items)


knowledge_recall_service = UnifiedKnowledgeRecallService()


__all__ = [
    "UnifiedKnowledgeRecallService",
    "knowledge_recall_service",
]
