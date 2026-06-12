import asyncio
import time

from fastapi import HTTPException

from app.core import constants
from app.core.i18n import t
from app.core.log import get_logger
from app.core.retrieval.fusion import reciprocal_rank_fusion
from app.core.retrieval.schemas import RetrievalChunk, RetrievalHit
from app.core.retrieval.sparse import bm25_search
from app.models.knowledge_base import KnowledgeBaseQueryTestItem, KnowledgeBaseQueryTestResponse
from app.providers.vector import get_collection, get_collection_items

HYBRID_DENSE_CANDIDATE_K = 20
HYBRID_SPARSE_CANDIDATE_K = 20
HYBRID_RRF_K = 60

logger = get_logger(__name__)


def parse_chroma_query_result(result: dict) -> list[RetrievalHit]:
    ids = result.get("ids", [[]])[0] if result.get("ids") else []
    documents = result.get("documents", [[]])[0] if result.get("documents") else []
    metadatas = result.get("metadatas", [[]])[0] if result.get("metadatas") else []
    distances = result.get("distances", [[]])[0] if result.get("distances") else []

    hits: list[RetrievalHit] = []
    for index, item_id in enumerate(ids):
        hits.append(
            RetrievalHit(
                id=item_id,
                content=documents[index] if index < len(documents) else "",
                metadata=metadatas[index] if index < len(metadatas) and metadatas[index] else {},
                dense_distance=distances[index] if index < len(distances) else None,
                dense_rank=index + 1,
            )
        )
    return hits


def parse_chroma_collection_items(result: dict) -> list[RetrievalChunk]:
    ids = result.get("ids") or []
    documents = result.get("documents") or []
    metadatas = result.get("metadatas") or []

    chunks: list[RetrievalChunk] = []
    for index, item_id in enumerate(ids):
        content = documents[index] if index < len(documents) and documents[index] else ""
        if not content:
            continue
        chunks.append(
            RetrievalChunk(
                id=item_id,
                content=content,
                metadata=metadatas[index] if index < len(metadatas) and metadatas[index] else {},
            )
        )
    return chunks


def dense_search(collection_name: str, query_embedding: list[float], candidate_k: int) -> list[RetrievalHit]:
    collection = get_collection(collection_name)
    dense_raw = collection.query(
        query_embeddings=[query_embedding],
        n_results=candidate_k,
        include=["documents", "metadatas", "distances"],
    )
    dense_hits = parse_chroma_query_result(dense_raw)
    return dense_hits


def sparse_search(collection_name: str, query: str, candidate_k: int) -> list[RetrievalHit]:
    collection_items = get_collection_items(collection_name, include=["documents", "metadatas"])
    chunks = parse_chroma_collection_items(collection_items)
    sparse_hits = bm25_search(query, chunks, candidate_k)
    return sparse_hits


async def hybrid_query_collection(collection_name: str, query_embedding: list[float], query: str, limit: int) -> list[RetrievalHit]:
    """通用候选召回函数：并发执行 dense/sparse 检索并使用 RRF 融合，返回 RetrievalHit 列表。

    注：参数 limit 表示拉取候选的数量限制（候选池大小），与最终截断数量 top_k 区分。
    本函数不负责响应包装，便于在 RRF 之后插入 reranker。
    """
    dense_candidate_k = max(limit, HYBRID_DENSE_CANDIDATE_K)
    sparse_candidate_k = max(limit, HYBRID_SPARSE_CANDIDATE_K)

    logger.bind(collection_name=collection_name, limit=limit, dense_candidate_k=dense_candidate_k, sparse_candidate_k=sparse_candidate_k, retrieval_stage="hybrid_started").info("混合检索开始：准备并发提交稠密检索和稀疏检索")

    dense_started = time.perf_counter()
    dense_task = asyncio.to_thread(dense_search, collection_name, query_embedding, dense_candidate_k)
    sparse_task = asyncio.to_thread(sparse_search, collection_name, query, sparse_candidate_k)
    dense_result, sparse_result = await asyncio.gather(dense_task, sparse_task, return_exceptions=True)
    recall_latency_ms = (time.perf_counter() - dense_started) * 1000

    if isinstance(dense_result, Exception):
        logger.bind(collection_name=collection_name, retrieval_type="dense").error(f"知识库稠密检索失败: {dense_result}")
        if isinstance(dense_result, HTTPException):
            raise dense_result
        raise HTTPException(status_code=500, detail=t(constants.ERR_KB_DENSE_RETRIEVAL_FAILED))

    dense_hits = dense_result
    logger.bind(collection_name=collection_name, candidate_k=dense_candidate_k, hit_count=len(dense_hits), retrieval_type="dense", retrieval_stage="finished").info(f"稠密检索完成：取到 {len(dense_hits)} 条信息")

    sparse_hits: list[RetrievalHit] = []
    if isinstance(sparse_result, Exception):
        logger.bind(collection_name=collection_name, retrieval_type="sparse").warning(f"知识库稀疏检索失败，已退化为纯稠密结果: {sparse_result}")
    else:
        sparse_hits = sparse_result
        logger.bind(collection_name=collection_name, candidate_k=sparse_candidate_k, hit_count=len(sparse_hits), retrieval_type="sparse", retrieval_stage="finished").info(f"稀疏检索完成：取到 {len(sparse_hits)} 条信息")

    rrf_started = time.perf_counter()
    fused_hits = reciprocal_rank_fusion(
        dense_results=dense_hits,
        sparse_results=sparse_hits,
        top_k=limit,
        rrf_k=HYBRID_RRF_K,
    )
    if not fused_hits:
        fused_hits = dense_hits[:limit]
    rrf_fusion_latency_ms = (time.perf_counter() - rrf_started) * 1000

    logger.bind(
        collection_name=collection_name,
        recall_latency_ms=round(recall_latency_ms, 2),
        rrf_fusion_latency_ms=round(rrf_fusion_latency_ms, 2),
        fused_count=len(fused_hits),
        retrieval_stage="fusion_finished",
    ).info("混合检索候选融合完成")

    return fused_hits


def build_query_test_response(hits: list[RetrievalHit], retrieval_mode: str, rerank_error: str | None = None) -> KnowledgeBaseQueryTestResponse:
    """将 RetrievalHit 列表包装为知识库测试响应，逐 item 写入解释字段，检索级信息上提为顶层字段。"""
    items = []
    for hit in hits:
        metadata = dict(hit.metadata or {})
        metadata.update(
            {
                "dense_rank": hit.dense_rank,
                "sparse_rank": hit.sparse_rank,
                "dense_distance": hit.dense_distance,
                "sparse_score": hit.sparse_score,
                "fusion_score": hit.fusion_score,
                "rerank_score": hit.rerank_score,
                "rerank_rank": hit.rerank_rank,
            }
        )
        items.append(
            KnowledgeBaseQueryTestItem(
                id=hit.id,
                content=hit.content,
                metadata_=metadata,
                distance=hit.dense_distance,
            )
        )

    return KnowledgeBaseQueryTestResponse(items=items, retrieval_mode=retrieval_mode, rerank_error=rerank_error)


async def hybrid_query_knowledge_base(collection_name: str, query_embedding: list[float], query: str, top_k: int) -> KnowledgeBaseQueryTestResponse:
    """兼容入口：纯混合检索（不走 rerank），保持原响应结构。"""
    fused_hits = await hybrid_query_collection(collection_name, query_embedding, query, limit=top_k)
    return build_query_test_response(fused_hits[:top_k], retrieval_mode="hybrid")
