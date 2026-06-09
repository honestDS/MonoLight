import asyncio

from fastapi import HTTPException

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


async def hybrid_query_knowledge_base(collection_name: str, query_embedding: list[float], query: str, top_k: int) -> KnowledgeBaseQueryTestResponse:
    dense_candidate_k = max(top_k, HYBRID_DENSE_CANDIDATE_K)
    sparse_candidate_k = max(top_k, HYBRID_SPARSE_CANDIDATE_K)

    logger.bind(collection_name=collection_name, top_k=top_k, dense_candidate_k=dense_candidate_k, sparse_candidate_k=sparse_candidate_k, retrieval_stage="hybrid_started").info("混合检索开始：准备并发提交稠密检索和稀疏检索")

    dense_task = asyncio.to_thread(dense_search, collection_name, query_embedding, dense_candidate_k)
    sparse_task = asyncio.to_thread(sparse_search, collection_name, query, sparse_candidate_k)
    dense_result, sparse_result = await asyncio.gather(dense_task, sparse_task, return_exceptions=True)

    if isinstance(dense_result, Exception):
        logger.bind(collection_name=collection_name, retrieval_type="dense").error(f"知识库稠密检索失败: {dense_result}")
        if isinstance(dense_result, HTTPException):
            raise dense_result
        raise HTTPException(status_code=500, detail=f"知识库稠密检索失败: {str(dense_result)}")

    dense_hits = dense_result
    logger.bind(collection_name=collection_name, candidate_k=dense_candidate_k, hit_count=len(dense_hits), retrieval_type="dense", retrieval_stage="finished").info(f"稠密检索完成：取到 {len(dense_hits)} 条信息")

    sparse_hits: list[RetrievalHit] = []
    if isinstance(sparse_result, Exception):
        logger.bind(collection_name=collection_name, retrieval_type="sparse").warning(f"知识库稀疏检索失败，已退化为纯稠密结果: {sparse_result}")
    else:
        sparse_hits = sparse_result
        logger.bind(collection_name=collection_name, candidate_k=sparse_candidate_k, hit_count=len(sparse_hits), retrieval_type="sparse", retrieval_stage="finished").info(f"稀疏检索完成：取到 {len(sparse_hits)} 条信息")

    fused_hits = reciprocal_rank_fusion(
        dense_results=dense_hits,
        sparse_results=sparse_hits,
        top_k=top_k,
        rrf_k=HYBRID_RRF_K,
    )
    if not fused_hits:
        fused_hits = dense_hits[:top_k]

    items = []
    for hit in fused_hits:
        metadata = dict(hit.metadata or {})
        metadata.update(
            {
                "retrieval_mode": "hybrid",
                "dense_rank": hit.dense_rank,
                "sparse_rank": hit.sparse_rank,
                "dense_distance": hit.dense_distance,
                "sparse_score": hit.sparse_score,
                "fusion_score": hit.fusion_score,
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

    return KnowledgeBaseQueryTestResponse(items=items)
