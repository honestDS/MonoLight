from sqlmodel.ext.asyncio.session import AsyncSession

from app.core import constants
from app.core.crud.provider import provider_crud
from app.core.exceptions import RerankException
from app.core.log import get_logger
from app.core.rerank.schemas import RerankConfig
from app.core.retrieval.schemas import RetrievalHit
from app.models.profile import Profile, ProfileConfig
from app.models.provider import ModelUsage
from app.providers.rerank import RerankClient

logger = get_logger(__name__)


async def get_profile_rerank_config(db: AsyncSession, profile: Profile) -> RerankConfig | None:
    """从 Profile 读取 rerank 配置；未配置或配置不完整时返回 None（视为不走 rerank）。

    启用判定：同时配置了有效的 rerank_provider_id 与 rerank_model_id 即视为启用 rerank；
    任一缺失则视为未启用，直接返回 None（不抛错），以兼容“未配置即关闭”的语义。
    """
    # 通过 ProfileConfig.model_validate 获得已校验、已填默认值的 provider 配置对象
    # 存量 Profile 的 configs 中可能不存在 rerank 键，model_validate 会按默认值补齐
    try:
        provider_config = ProfileConfig.model_validate(profile.configs or {}).provider
    except Exception as e:
        logger.bind(profile_id=profile.id).warning(f"解析 Profile 配置失败，跳过 rerank: {e}")
        return None

    rerank_provider_id = provider_config.rerank_provider_id
    rerank_model_id = provider_config.rerank_model_id

    # 未配置 rerank 提供商或模型，视为未启用 rerank
    if not rerank_provider_id or rerank_provider_id <= 0 or not rerank_model_id:
        return None

    provider = await provider_crud.get(db, rerank_provider_id)
    if not provider:
        raise RerankException(constants.ERR_PROFILE_RERANK_PROVIDER_NOT_FOUND)
    if provider.usage != ModelUsage.RERANK:
        raise RerankException(constants.ERR_PROFILE_PROVIDER_NOT_RERANK)
    if not provider.is_active:
        raise RerankException(constants.ERR_PROFILE_RERANK_PROVIDER_DISABLED)
    if not provider.base_url:
        raise RerankException(constants.ERR_PROFILE_RERANK_PROVIDER_NO_URL)

    return RerankConfig(
        provider_type=provider.provider_type,
        api_key=provider.api_key,
        base_url=provider.base_url,
        model_id=rerank_model_id,
        candidate_k=provider_config.rerank_candidate_k,
        timeout=provider_config.rerank_timeout,
    )


async def rerank_retrieval_hits(
    config: RerankConfig,
    query: str,
    hits: list[RetrievalHit],
    final_top_k: int,
) -> list[RetrievalHit]:
    """调用远程 reranker 对候选 hits 精排，并把分数与排名回填到 RetrievalHit。

    回填规则：
    - 远程响应 results[].index 指向本次请求 documents 数组下标，与传入 hits 顺序严格一一对应。
    - 命中的候选按 relevance_score 降序排在前面，并写入 rerank_score / rerank_rank。
    - 未被远程命中的候选按原 RRF 顺序补齐，rerank_score / rerank_rank 保持 None。
    """
    if not hits:
        return []

    # documents 顺序必须与 hits 严格一一对应，构造后不得改变顺序
    documents = [hit.content for hit in hits]
    results = await RerankClient.rerank_texts(
        provider_type=config.provider_type,
        api_key=config.api_key,
        base_url=config.base_url,
        model_id=config.model_id,
        query=query,
        documents=documents,
        top_n=final_top_k,
        timeout=config.timeout,
    )

    # 远程返回空结果：回退到原 RRF 顺序
    if not results:
        return hits

    reranked: list[RetrievalHit] = []
    used_indexes: set[int] = set()
    rank = 1
    for result in results:
        index = result.index
        # 防御越界或重复 index，丢弃非法项
        if index < 0 or index >= len(hits) or index in used_indexes:
            logger.bind(index=index, hit_count=len(hits)).warning("Rerank 返回的 index 非法或重复，已丢弃")
            continue
        hit = hits[index]
        hit.rerank_score = result.relevance_score
        hit.rerank_rank = rank
        reranked.append(hit)
        used_indexes.add(index)
        rank += 1

    # 部分结果：未被远程命中的候选按原 RRF 顺序补齐
    for index, hit in enumerate(hits):
        if index not in used_indexes:
            reranked.append(hit)

    return reranked
