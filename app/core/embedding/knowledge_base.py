"""Embedding 知识库：渠道管理架构适配版"""

import time
from typing import Any

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.constants import (
    ERR_KB_DENSE_RETRIEVAL_FAILED,
    ERR_KB_NOT_FOUND_FOR_QUERY,
    ERR_KB_NOT_IN_PROFILE,
    LOG_KNOWLEDGE_RECALL_CANDIDATE_WINDOW_EXPANDED,
)
from app.core.embedding.common import embed_texts_with_config, load_embedding_runtime_config
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.knowledge.recall import (
    filter_recallable_managed_hits,
    materialize_recallable_managed_hits,
)
from app.core.log import get_logger
from app.core.rerank.knowledge_base import get_profile_rerank_config, rerank_retrieval_hits
from app.core.retrieval.hybrid import build_query_test_response, hybrid_query_collection
from app.core.retrieval.schemas import RetrievalHit
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseProfileBinding, KnowledgeBaseQueryTestResponse, KnowledgeBaseType
from app.models.profile import Profile, ProfileConfig

KNOWLEDGE_BASE_QUERY_TOP_K = 5

logger = get_logger(__name__)


def get_profile_kb_query_top_k(profile: Profile) -> int:
    """读取 Profile 配置中的知识库检索最终返回数量。"""
    try:
        top_k = ProfileConfig.model_validate(profile.configs or {}).memory.knowledge.top_k
    except Exception:
        return KNOWLEDGE_BASE_QUERY_TOP_K
    if top_k <= 0:
        return KNOWLEDGE_BASE_QUERY_TOP_K
    return min(top_k, 50)


async def embed_chunks_with_knowledge_base_config(
    db: AsyncSession,
    kb: KnowledgeBase,
    texts: list[str],
    batch_size: int,
    *,
    release_connection: bool = False,
) -> list[list[float]]:
    active_embedding = resolve_active_knowledge_base_embedding(kb)
    config = await load_embedding_runtime_config(db, active_embedding.channel_id, active_embedding.model_id)
    return await embed_texts_with_config(
        config,
        texts,
        batch_size=batch_size,
        dimensions=active_embedding.dimensions,
        db=db,
        release_connection=release_connection,
    )


async def list_available_knowledge_bases(db: AsyncSession, profile: Profile) -> list[KnowledgeBase]:
    """查询当前所选 Profile 可用的知识库"""
    query = (
        select(KnowledgeBase)
        .join(
            KnowledgeBaseProfileBinding,
            KnowledgeBaseProfileBinding.knowledge_base_id == KnowledgeBase.id,
        )
        .where(KnowledgeBaseProfileBinding.profile_id == profile.id)
        .where(KnowledgeBaseProfileBinding.uid == profile.uid)
        .where(KnowledgeBase.uid == profile.uid)
        .order_by(KnowledgeBase.created_at.desc())
    )
    result = await db.execute(query)
    return list(result.scalars().all())


def build_knowledge_base_prompt_items(kbs: list[KnowledgeBase]) -> list[dict[str, Any]]:
    """将知识库实体转换为系统提示词清单项"""
    prompt_items = []
    for knowledge_base in kbs:
        prompt_items.append(
            {
                "id": knowledge_base.id,
                "name": knowledge_base.name,
                "description": knowledge_base.description or "",
                "kind": ("managed_knowledge" if knowledge_base.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED else "user_knowledge_base"),
            }
        )
    return prompt_items


def build_knowledge_base_whitelist(kbs: list[KnowledgeBase]) -> list[int]:
    """生成当前 Profile 可用知识库 ID 白名单"""
    whitelist_ids = []
    for knowledge_base in kbs:
        if knowledge_base.id is not None:
            whitelist_ids.append(knowledge_base.id)
    return whitelist_ids


async def _query_recallable_candidates(
    db: AsyncSession,
    *,
    profile_uid: str,
    knowledge_base_id: int,
    collection_name: str,
    query_embedding: list[float],
    query: str,
    target_count: int,
) -> list[RetrievalHit]:
    """逐步扩大候选窗口，避免失效 managed 向量占满 top-k 后造成有效结果缺失。"""
    requested = max(target_count, 1)
    previous_raw_count = -1
    while True:
        await db.commit()
        raw_hits = await hybrid_query_collection(
            collection_name,
            query_embedding,
            query,
            limit=requested,
            error_key=ERR_KB_DENSE_RETRIEVAL_FAILED,
        )
        filtered_hits = await filter_recallable_managed_hits(
            db,
            uid=profile_uid,
            knowledge_base_id=knowledge_base_id,
            hits=raw_hits,
        )
        await db.commit()
        if len(filtered_hits) >= target_count:
            return filtered_hits
        raw_count = len(raw_hits)
        if raw_count < requested or raw_count <= previous_raw_count:
            return filtered_hits
        previous_raw_count = raw_count
        next_requested = requested * 2
        logger.bind(
            knowledge_base_id=knowledge_base_id,
            previous_limit=requested,
            next_limit=next_requested,
            valid_count=len(filtered_hits),
            target_count=target_count,
        ).info(
            t(
                LOG_KNOWLEDGE_RECALL_CANDIDATE_WINDOW_EXPANDED,
                previous_limit=requested,
                next_limit=next_requested,
                valid_count=len(filtered_hits),
                target_count=target_count,
            )
        )
        requested = next_requested


async def _build_final_query_response(
    db: AsyncSession,
    *,
    profile_uid: str,
    knowledge_base_id: int,
    hits: list[RetrievalHit],
    final_top_k: int,
    retrieval_mode: str,
    rerank_error: str | None = None,
) -> KnowledgeBaseQueryTestResponse:
    materialized_hits = await materialize_recallable_managed_hits(
        db,
        uid=profile_uid,
        knowledge_base_id=knowledge_base_id,
        hits=hits,
    )
    return build_query_test_response(
        materialized_hits[:final_top_k],
        retrieval_mode=retrieval_mode,
        rerank_error=rerank_error,
    )


async def query_knowledge_base(
    db: AsyncSession,
    profile: Profile,
    kb_id: int,
    query: str,
    top_k: int = KNOWLEDGE_BASE_QUERY_TOP_K,
    expose_rerank_error: bool = False,
    require_binding: bool = True,
) -> KnowledgeBaseQueryTestResponse:
    """根据知识库 ID 检索知识库。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=ERR_KB_NOT_FOUND_FOR_QUERY)

    if kb.uid != profile.uid:
        raise HTTPException(status_code=403, detail=ERR_KB_NOT_IN_PROFILE)

    if require_binding:
        binding_result = await db.execute(select(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.knowledge_base_id == kb_id).where(KnowledgeBaseProfileBinding.profile_id == profile.id).where(KnowledgeBaseProfileBinding.uid == profile.uid))
        if not binding_result.scalars().first():
            raise HTTPException(status_code=403, detail=ERR_KB_NOT_IN_PROFILE)

    active_embedding = resolve_active_knowledge_base_embedding(kb)
    query_embedding = (await embed_chunks_with_knowledge_base_config(db, kb, [query], 1, release_connection=True))[0]
    final_top_k = top_k

    excluded_rerank_priorities: set[int] = set()
    rerank_error: str | None = None
    rerank_attempted = False

    while True:
        try:
            rerank_config = await get_profile_rerank_config(db, profile, excluded_priorities=excluded_rerank_priorities)
        except LLMException as e:
            rerank_error = t(e.message, default=e.message, **e.kwargs)
            logger.bind(profile_id=profile.id, kb_id=kb_id).warning(t("LOG_RERANK_CONFIG_READ_FAILED", error=t(e.message, default=e.message, **e.kwargs)))
            break

        if rerank_config is None:
            break

        rerank_attempted = True
        effective_candidate_k = max(rerank_config.candidate_k, final_top_k)
        fused_hits = await _query_recallable_candidates(
            db,
            profile_uid=profile.uid,
            knowledge_base_id=kb_id,
            collection_name=active_embedding.collection_name,
            query_embedding=query_embedding,
            query=query,
            target_count=effective_candidate_k,
        )

        if len(fused_hits) <= final_top_k:
            return await _build_final_query_response(
                db,
                profile_uid=profile.uid,
                knowledge_base_id=kb_id,
                hits=fused_hits,
                final_top_k=final_top_k,
                retrieval_mode="hybrid",
            )

        try:
            rerank_started = time.perf_counter()
            reranked_hits = await rerank_retrieval_hits(rerank_config, query, fused_hits, final_top_k)
            rerank_latency_ms = (time.perf_counter() - rerank_started) * 1000
            logger.bind(
                kb_id=kb_id,
                candidate_count=len(fused_hits),
                final_top_k=final_top_k,
                rerank_model_id=rerank_config.model_id,
                rerank_latency_ms=round(rerank_latency_ms, 2),
            ).info(t("LOG_RERANK_REMOTE_FINISHED"))
            return await _build_final_query_response(
                db,
                profile_uid=profile.uid,
                knowledge_base_id=kb_id,
                hits=reranked_hits,
                final_top_k=final_top_k,
                retrieval_mode="hybrid_rerank",
            )

        except LLMException as e:
            excluded_rerank_priorities.add(rerank_config.priority)
            rerank_error = t(e.message, default=e.message, **e.kwargs)
            rerank_channel_name = getattr(rerank_config, "channel_name", None)
            logger.bind(
                kb_id=kb_id,
                rerank_channel_id=getattr(rerank_config, "channel_id", None),
                rerank_channel_name=rerank_channel_name,
                rerank_model_id=rerank_config.model_id,
                rerank_model_name=rerank_config.model_id,
                rerank_channel_display_name=f"{rerank_channel_name} / {rerank_config.model_id}",
            ).warning(t("LOG_RERANK_REMOTE_CALL_FAILED", error=t(e.message, default=e.message, **e.kwargs)))

    if rerank_attempted and rerank_error and expose_rerank_error:
        raise HTTPException(status_code=502, detail=rerank_error)

    fused_hits = await _query_recallable_candidates(
        db,
        profile_uid=profile.uid,
        knowledge_base_id=kb_id,
        collection_name=active_embedding.collection_name,
        query_embedding=query_embedding,
        query=query,
        target_count=final_top_k,
    )
    return await _build_final_query_response(
        db,
        profile_uid=profile.uid,
        knowledge_base_id=kb_id,
        hits=fused_hits,
        final_top_k=final_top_k,
        retrieval_mode="hybrid",
        rerank_error=rerank_error if expose_rerank_error else None,
    )
