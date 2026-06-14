import time
from typing import Any

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core import constants
from app.core.crud.provider import provider_crud
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.rerank.knowledge_base import get_profile_rerank_config, rerank_retrieval_hits
from app.core.retrieval.hybrid import build_query_test_response, hybrid_query_collection
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseQueryTestResponse
from app.models.profile import Profile
from app.models.provider import ModelUsage, ProviderType
from app.providers.embedding import EmbeddingClient

KNOWLEDGE_BASE_QUERY_TOP_K = 5

logger = get_logger(__name__)


def get_profile_kb_query_top_k(profile: Profile) -> int:
    """读取 Profile 配置的对话工具知识库检索最终返回数量。

    存量 Profile 的 configs 中可能不存在该键，缺键时回退默认值 KNOWLEDGE_BASE_QUERY_TOP_K。
    同时对取值做合法性裁剪（1~50），避免存量脏数据导致越界。
    """
    provider_config = (profile.configs or {}).get("provider", {})
    raw_top_k = provider_config.get("kb_query_top_k")
    if not isinstance(raw_top_k, int) or raw_top_k <= 0:
        return KNOWLEDGE_BASE_QUERY_TOP_K
    return min(raw_top_k, 50)


async def get_profile_embedding_config(db: AsyncSession, profile: Profile) -> tuple[ProviderType, str, str, str, int | None]:
    provider_config = (profile.configs or {}).get("provider", {})
    embedding_provider_id = provider_config.get("embedding_provider_id")
    embedding_model_id = provider_config.get("embedding_model_id")
    embedding_dimensions = provider_config.get("embedding_dimensions")

    if not embedding_provider_id or embedding_provider_id <= 0:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_NO_EMBEDDING_PROVIDER)
    if not embedding_model_id:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_NO_EMBEDDING_MODEL)

    provider = await provider_crud.get(db, embedding_provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail=constants.ERR_PROFILE_EMBEDDING_PROVIDER_NOT_FOUND)
    if provider.usage != ModelUsage.EMBEDDING:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_PROVIDER_NOT_EMBEDDING)
    if not provider.is_active:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_EMBEDDING_PROVIDER_DISABLED)
    if not provider.base_url:
        raise HTTPException(status_code=400, detail=constants.ERR_PROFILE_EMBEDDING_PROVIDER_NO_URL)
    return provider.provider_type, provider.api_key, provider.base_url, embedding_model_id, embedding_dimensions


async def is_embedding_profile_available(db: AsyncSession, profile: Profile) -> bool:
    try:
        await get_profile_embedding_config(db, profile)
        return True
    except HTTPException:
        return False


def get_profile_embedding_timeout(profile: Profile) -> float:
    """读取 Profile 配置的嵌入模型调用超时（秒）。

    存量 Profile 缺键时回退默认值 30 秒，并对取值做合法性裁剪（0~600）。
    """
    provider_config = (profile.configs or {}).get("provider", {})
    raw_timeout = provider_config.get("embedding_timeout")
    if not isinstance(raw_timeout, int | float) or raw_timeout <= 0:
        return 30.0
    return min(float(raw_timeout), 600.0)


async def embed_chunks(db: AsyncSession, profile: Profile, texts: list[str], batch_size: int) -> list[list[float]]:
    provider_type, api_key, base_url, model_id, dimensions = await get_profile_embedding_config(db, profile)
    embedding_timeout = get_profile_embedding_timeout(profile)
    try:
        return await EmbeddingClient.embed_texts(
            provider_type=provider_type,
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            input_texts=texts,
            batch_size=batch_size,
            dimensions=dimensions,
            timeout=embedding_timeout,
        )
    except LLMException as e:
        raise HTTPException(status_code=502, detail=t(constants.ERR_PROFILE_EMBEDDING_CALL_FAILED, message=e.message))


async def list_available_knowledge_bases(db: AsyncSession, profile: Profile) -> list[KnowledgeBase]:
    """查询当前激活 Profile 可用的知识库"""
    query = select(KnowledgeBase).where(KnowledgeBase.profile_id == profile.id).order_by(KnowledgeBase.created_at.desc())
    result = await db.execute(query)
    return list(result.scalars().all())


def build_knowledge_base_prompt_items(kbs: list[KnowledgeBase]) -> list[dict[str, Any]]:
    """将知识库实体转换为系统提示词清单项，输出 id、name 与 description"""
    return [
        {
            "id": kb.id,
            "name": kb.name,
            "description": kb.description or "",
        }
        for kb in kbs
    ]


def build_knowledge_base_whitelist(kbs: list[KnowledgeBase]) -> list[int]:
    """生成当前 Profile 可用知识库 ID 白名单"""
    return [kb.id for kb in kbs if kb.id is not None]


async def query_knowledge_base(
    db: AsyncSession,
    profile: Profile,
    kb_id: int,
    query: str,
    top_k: int = KNOWLEDGE_BASE_QUERY_TOP_K,
    expose_rerank_error: bool = False,
) -> KnowledgeBaseQueryTestResponse:
    """根据知识库 ID 检索知识库。

    检索流程：dense/sparse 召回 -> RRF 融合 -> 可选远程 reranker 精排 -> final_top_k 截断。

    参数：
    - top_k：最终返回数量（final_top_k），由调用方入参传入（query-test 传用户入参，对话工具传固定值）。
    - expose_rerank_error：是否把 rerank 降级原因回填到响应（query-test 路径传 True，对话工具路径保持 False 静默降级）。
    """
    kb = await db.get(KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail=constants.ERR_KB_NOT_FOUND_FOR_QUERY)

    if kb.profile_id != profile.id:
        raise HTTPException(status_code=403, detail=constants.ERR_KB_NOT_IN_PROFILE)

    query_embedding = (await embed_chunks(db, profile, [query], 1))[0]
    final_top_k = top_k

    # 读取 rerank 配置；配置缺失/异常时降级（不阻断检索）
    rerank_config = None
    rerank_error: str | None = None
    try:
        rerank_config = await get_profile_rerank_config(db, profile)
    except LLMException as e:
        rerank_error = t(e.message, default=e.message, **e.kwargs)
        logger.bind(profile_id=profile.id, kb_id=kb_id).warning(f"读取 rerank 配置失败，降级为纯混合检索: {e.message}")

    # 未启用 rerank：保持原 hybrid 行为
    if rerank_config is None:
        fused_hits = await hybrid_query_collection(kb.collection_name, query_embedding, query, limit=final_top_k)
        return build_query_test_response(
            fused_hits[:final_top_k],
            retrieval_mode="hybrid",
            rerank_error=rerank_error if expose_rerank_error else None,
        )

    # 启用 rerank：扩大候选池到 effective_candidate_k
    effective_candidate_k = max(rerank_config.candidate_k, final_top_k)
    fused_hits = await hybrid_query_collection(kb.collection_name, query_embedding, query, limit=effective_candidate_k)

    # 候选不足以改变最终返回集合时，短路跳过远程 rerank，按 RRF 顺序返回
    if len(fused_hits) <= final_top_k:
        return build_query_test_response(fused_hits[:final_top_k], retrieval_mode="hybrid")

    # 调用远程 reranker，失败时降级回退 RRF 结果
    try:
        rerank_started = time.perf_counter()
        reranked_hits = await rerank_retrieval_hits(rerank_config, query, fused_hits, final_top_k)
        rerank_latency_ms = (time.perf_counter() - rerank_started) * 1000
        logger.bind(
            kb_id=kb_id,
            candidate_count=len(fused_hits),
            final_top_k=final_top_k,
            rerank_provider_type=str(rerank_config.provider_type),
            rerank_model_id=rerank_config.model_id,
            rerank_latency_ms=round(rerank_latency_ms, 2),
        ).info("远程 rerank 精排完成")
        return build_query_test_response(reranked_hits[:final_top_k], retrieval_mode="hybrid_rerank")

    except LLMException as e:
        logger.bind(kb_id=kb_id, rerank_model_id=rerank_config.model_id).warning(f"远程 rerank 调用失败，降级为纯混合检索: {e.message}")
        return build_query_test_response(
            fused_hits[:final_top_k],
            retrieval_mode="hybrid",
            rerank_error=t(e.message, default=e.message, **e.kwargs) if expose_rerank_error else None,
        )
