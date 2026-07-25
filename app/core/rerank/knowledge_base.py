"""Rerank 知识库：渠道管理架构适配版

get_profile_rerank_config() 走 rerank_channel 路由；超时从 model_entry 读取
"""

from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.channel_router import select_channel
from app.core.constants import ERR_PROFILE_RERANK_CHANNEL_NO_URL
from app.core.exceptions import RerankException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.rerank.schemas import RerankConfig
from app.core.retrieval.schemas import RetrievalHit
from app.models.channel import ChannelConfig, resolve_model_protocol
from app.models.profile import Profile
from app.providers.rerank import RerankClient

logger = get_logger(__name__)


async def get_profile_rerank_config(
    db: AsyncSession,
    profile: Profile,
    excluded_priorities: set[int] | None = None,
) -> RerankConfig | None:
    """从 Profile 的 rerank_channel 中通过渠道路由获取 rerank 配置。

    未配置或路由无结果时返回 None（视为不走 rerank）。
    """
    channel_config = (profile.configs or {}).get("channel", {})
    rerank_channel_raw = channel_config.get("rerank_channel")

    if not rerank_channel_raw:
        return None

    try:
        rerank_channel = ChannelConfig.model_validate(rerank_channel_raw)
    except Exception as e:
        logger.bind(profile_id=profile.id).warning(t("LOG_RERANK_CONFIG_PARSE_FAILED", error=str(e)))
        return None

    if not rerank_channel or not rerank_channel.rules:
        return None

    selection = await select_channel(
        db,
        rerank_channel,
        "RERANK",
        call_context="knowledge_base_rerank",
        excluded_priorities=excluded_priorities,
        cursor_key=f"{profile.id}:RERANK",
    )
    if not selection:
        return None

    channel, model_entry, _rule = selection

    if not channel.base_url:
        raise RerankException(ERR_PROFILE_RERANK_CHANNEL_NO_URL)

    return RerankConfig(
        channel_id=channel.id,
        channel_name=channel.name,
        api_key=channel.get_decrypted_api_key(),
        base_url=channel.base_url,
        model_id=model_entry["model_id"],
        protocol=resolve_model_protocol(model_entry),
        candidate_k=rerank_channel.rerank_candidate_k,
        timeout=model_entry.get("rerank_timeout") if model_entry.get("rerank_timeout") is not None else rerank_channel.rerank_timeout,
        priority=_rule.priority,
    )


async def rerank_retrieval_hits(
    config: RerankConfig,
    query: str,
    hits: list[RetrievalHit],
    final_top_k: int,
) -> list[RetrievalHit]:
    """调用远程 reranker 对候选 hits 精排，并把分数与排名回填到 RetrievalHit。"""
    if not hits:
        return []

    documents = [hit.content for hit in hits]
    results = await RerankClient.rerank_texts(
        api_key=config.api_key,
        base_url=config.base_url,
        model_id=config.model_id,
        protocol=config.protocol,
        query=query,
        documents=documents,
        top_n=final_top_k,
        timeout=config.timeout,
    )

    if not results:
        return hits

    reranked: list[RetrievalHit] = []
    used_indexes: set[int] = set()
    rank = 1
    for result in results:
        index = result.index
        if index < 0 or index >= len(hits) or index in used_indexes:
            logger.bind(index=index, hit_count=len(hits)).warning(t("LOG_RERANK_INDEX_INVALID"))
            continue
        hit = hits[index]
        hit.rerank_score = result.relevance_score
        hit.rerank_rank = rank
        reranked.append(hit)
        used_indexes.add(index)
        rank += 1

    for index, hit in enumerate(hits):
        if index not in used_indexes:
            reranked.append(hit)

    return reranked
