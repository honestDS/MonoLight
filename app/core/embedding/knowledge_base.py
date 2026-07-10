"""Embedding 知识库：渠道管理架构适配版"""

import time
from typing import Any

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core import constants
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.rerank.knowledge_base import get_profile_rerank_config, rerank_retrieval_hits
from app.core.retrieval.hybrid import build_query_test_response, hybrid_query_collection
from app.models.channel import ChannelConfig, ModelChannel, ModelUsage
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseProfileBinding, KnowledgeBaseQueryTestResponse
from app.models.profile import Profile
from app.providers.embedding import EmbeddingClient

KNOWLEDGE_BASE_QUERY_TOP_K = 5

logger = get_logger(__name__)


def _get_channel_config(profile: Profile, channel_key: str) -> ChannelConfig | None:
    channel_config = (profile.configs or {}).get("channel", {})
    channel_raw = channel_config.get(channel_key)
    if not channel_raw:
        return None
    try:
        return ChannelConfig.model_validate(channel_raw)
    except Exception:
        return None


def get_profile_kb_query_top_k(profile: Profile) -> int:
    """读取重排渠道配置的知识库检索最终返回数量。"""
    rerank_channel = _get_channel_config(profile, "rerank_channel")
    if not rerank_channel or rerank_channel.kb_query_top_k <= 0:
        return KNOWLEDGE_BASE_QUERY_TOP_K
    return min(rerank_channel.kb_query_top_k, 50)


async def embed_chunks_with_knowledge_base_config(
    db: AsyncSession,
    kb: KnowledgeBase,
    texts: list[str],
    batch_size: int,
) -> list[list[float]]:
    channel = await db.get(ModelChannel, kb.embedding_channel_id)
    if not channel:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_EMBEDDING_CHANNEL_NOT_FOUND)
    if not channel.is_active:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_EMBEDDING_CHANNEL_DISABLED)
    if not channel.base_url:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_EMBEDDING_CHANNEL_NO_URL)

    model_entry = None
    for item in channel.model_ids or []:
        if item.get("model_id") == kb.embedding_model_id and item.get("usage") == ModelUsage.EMBEDDING and item.get("is_enabled", True):
            model_entry = item
            break
    if not model_entry:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_NO_EMBEDDING_MODEL)

    model_timeout = model_entry.get("embedding_timeout")
    embedding_timeout = min(float(model_timeout), 600.0) if model_timeout else 30.0
    return await EmbeddingClient.embed_texts(
        channel_type=channel.channel_type,
        api_key=channel.get_decrypted_api_key(),
        base_url=channel.base_url,
        model_id=kb.embedding_model_id,
        input_texts=texts,
        batch_size=batch_size,
        dimensions=kb.embedding_dimensions,
        timeout=embedding_timeout,
    )


async def list_available_knowledge_bases(db: AsyncSession, profile: Profile) -> list[KnowledgeBase]:
    """查询当前激活 Profile 可用的知识库"""
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
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND_FOR_QUERY)

    if require_binding:
        binding_result = await db.execute(select(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.knowledge_base_id == kb_id).where(KnowledgeBaseProfileBinding.profile_id == profile.id))
        if not binding_result.scalars().first():
            raise HTTPException(status_code=403, detail=constants.ERR_KB_NOT_IN_PROFILE)

    query_embedding = (await embed_chunks_with_knowledge_base_config(db, kb, [query], 1))[0]
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
        fused_hits = await hybrid_query_collection(kb.collection_name, query_embedding, query, limit=effective_candidate_k)

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
                rerank_channel_type=str(rerank_config.channel_type),
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

    fused_hits = await hybrid_query_collection(kb.collection_name, query_embedding, query, limit=final_top_k)
    return build_query_test_response(
        fused_hits[:final_top_k],
        retrieval_mode="hybrid",
        rerank_error=rerank_error if expose_rerank_error else None,
    )
