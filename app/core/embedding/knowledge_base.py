"""Embedding 知识库：渠道管理架构适配版

get_profile_embedding_config() 走 embedding_channel 路由；
embed_chunks() 超时从 model_entry 读取
"""

import time
from typing import Any

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core import constants
from app.core.channel_router import select_channel
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.rerank.knowledge_base import get_profile_rerank_config, rerank_retrieval_hits
from app.core.retrieval.hybrid import build_query_test_response, hybrid_query_collection
from app.models.channel import ChannelConfig
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseQueryTestResponse
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


async def get_profile_embedding_config(db: AsyncSession, profile: Profile, call_context: str = "knowledge_base_embedding_config", log_selection: bool = True) -> tuple:
    """从 Profile 的 embedding_channel 中通过渠道路由获取嵌入配置。

    Returns:
        (channel_type, api_key, base_url, model_id, dimensions)
    """
    channel_config = (profile.configs or {}).get("channel", {})
    embedding_channel_raw = channel_config.get("embedding_channel")

    if not embedding_channel_raw:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_NO_EMBEDDING_CHANNEL)

    try:
        embedding_channel = ChannelConfig.model_validate(embedding_channel_raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    selection = await select_channel(db, embedding_channel, "EMBEDDING", call_context=call_context, log_selection=log_selection)
    if not selection:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_NO_EMBEDDING_CHANNEL)

    channel, model_entry, _rule = selection
    model_id = model_entry.get("model_id")
    if not model_id:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_NO_EMBEDDING_MODEL)
    if not channel.base_url:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_EMBEDDING_CHANNEL_NO_URL)

    dimensions = model_entry.get("embedding_dimensions")
    return channel.channel_type, channel.get_decrypted_api_key(), channel.base_url, model_id, dimensions


async def is_embedding_profile_available(db: AsyncSession, profile: Profile) -> bool:
    try:
        # 仅为判断是否暴露知识库工具的可用性探测，非真实嵌入调用，静默不打"选择渠道"日志
        await get_profile_embedding_config(db, profile, call_context="knowledge_base_profile_availability_check", log_selection=False)
        return True
    except HTTPException:
        return False


def get_profile_embedding_timeout(profile: Profile) -> float:
    """读取嵌入渠道配置的模型调用超时（秒）。"""
    embedding_channel = _get_channel_config(profile, "embedding_channel")
    if not embedding_channel or embedding_channel.embedding_timeout <= 0:
        return 30.0
    return min(float(embedding_channel.embedding_timeout), 600.0)


async def embed_chunks(
    db: AsyncSession,
    profile: Profile,
    texts: list[str],
    batch_size: int,
    call_context: str = "knowledge_base_embedding",
) -> list[list[float]]:
    channel_config = (profile.configs or {}).get("channel", {})
    embedding_channel_raw = channel_config.get("embedding_channel")
    if not embedding_channel_raw:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_NO_EMBEDDING_CHANNEL)

    try:
        embedding_channel = ChannelConfig.model_validate(embedding_channel_raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    excluded_priorities: set[int] = set()
    last_error: LLMException | None = None

    while True:
        selection = await select_channel(
            db,
            embedding_channel,
            "EMBEDDING",
            call_context=call_context,
            excluded_priorities=excluded_priorities,
            cursor_key=f"{profile.id}:EMBEDDING",
        )
        if not selection:
            if last_error:
                raise HTTPException(status_code=502, detail=t(constants.ERR_PROFILE_EMBEDDING_CALL_FAILED, message=last_error.message))
            raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_NO_EMBEDDING_CHANNEL)

        channel, model_entry, _rule = selection
        model_id = model_entry.get("model_id")
        if not model_id:
            excluded_priorities.add(_rule.priority)
            continue
        if not channel.base_url:
            excluded_priorities.add(_rule.priority)
            continue

        model_timeout = model_entry.get("embedding_timeout")
        embedding_timeout = min(float(model_timeout), 600.0) if model_timeout else get_profile_embedding_timeout(profile)

        try:
            return await EmbeddingClient.embed_texts(
                channel_type=channel.channel_type,
                api_key=channel.get_decrypted_api_key(),
                base_url=channel.base_url,
                model_id=model_id,
                input_texts=texts,
                batch_size=batch_size,
                dimensions=model_entry.get("embedding_dimensions"),
                timeout=embedding_timeout,
            )
        except LLMException as e:
            last_error = e
            excluded_priorities.add(_rule.priority)
            logger.bind(
                profile_id=profile.id,
                channel_id=channel.id,
                channel_name=f"{channel.name} / {model_id}",
                model_id=model_id,
                model_name=model_id,
            ).warning(t("LOG_EMBEDDING_CHANNEL_FAILED", error=t(e.message, default=e.message, **e.kwargs)))


async def list_available_knowledge_bases(db: AsyncSession, profile: Profile) -> list[KnowledgeBase]:
    """查询当前激活 Profile 可用的知识库"""
    query = select(KnowledgeBase).where(KnowledgeBase.profile_id == profile.id).order_by(KnowledgeBase.created_at.desc())
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
) -> KnowledgeBaseQueryTestResponse:
    """根据知识库 ID 检索知识库。"""
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND_FOR_QUERY)

    if kb.profile_id != profile.id:
        raise HTTPException(status_code=403, detail=constants.ERR_KB_NOT_IN_PROFILE)

    query_embedding = (await embed_chunks(db, profile, [query], 1, call_context="knowledge_base_query_embedding"))[0]
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
