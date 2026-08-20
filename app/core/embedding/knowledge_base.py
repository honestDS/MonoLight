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
)
from app.core.embedding.common import embed_texts_with_config, load_embedding_runtime_config
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.rerank.knowledge_base import get_profile_rerank_config, rerank_retrieval_hits
from app.core.retrieval.hybrid import build_query_test_response, hybrid_query_collection
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseProfileBinding, KnowledgeBaseQueryTestResponse
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
    config = await load_embedding_runtime_config(db, kb.embedding_channel_id, kb.embedding_model_id)
    return await embed_texts_with_config(
        config,
        texts,
        batch_size=batch_size,
        dimensions=kb.embedding_dimensions,
        db=db,
        release_connection=release_connection,
    )


async def list_available_knowledge_bases(db: AsyncSession, profile: Profile) -> list[KnowledgeBase]:
    """查询当前所选 Profile 可用的知识库"""
    query = select(KnowledgeBase).join(KnowledgeBaseProfileBinding, KnowledgeBaseProfileBinding.knowledge_base_id == KnowledgeBase.id).where(KnowledgeBaseProfileBinding.profile_id == profile.id).order_by(KnowledgeBase.created_at.desc())
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

    if require_binding:
        binding_result = await db.execute(select(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.knowledge_base_id == kb_id).where(KnowledgeBaseProfileBinding.profile_id == profile.id))
        if not binding_result.scalars().first():
            raise HTTPException(status_code=403, detail=ERR_KB_NOT_IN_PROFILE)

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
        await db.commit()
        fused_hits = await hybrid_query_collection(kb.collection_name, query_embedding, query, limit=effective_candidate_k, error_key=ERR_KB_DENSE_RETRIEVAL_FAILED)

        if len(fused_hits) <= final_top_k:
            return build_query_test_response(fused_hits[:final_top_k], retrieval_mode="hybrid")

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
            return build_query_test_response(reranked_hits[:final_top_k], retrieval_mode="hybrid_rerank")

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

    await db.commit()
    fused_hits = await hybrid_query_collection(kb.collection_name, query_embedding, query, limit=final_top_k, error_key=ERR_KB_DENSE_RETRIEVAL_FAILED)
    return build_query_test_response(
        fused_hits[:final_top_k],
        retrieval_mode="hybrid",
        rerank_error=rerank_error if expose_rerank_error else None,
    )
