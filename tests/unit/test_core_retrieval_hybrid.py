from app.core.retrieval.fusion import reciprocal_rank_fusion
from app.core.retrieval.hybrid import parse_chroma_collection_items, parse_chroma_query_result
from app.core.retrieval.schemas import RetrievalChunk, RetrievalHit
from app.core.retrieval.sparse import bm25_search
from app.core.retrieval.tokenizer import tokenize_for_sparse_search


def test_tokenize_for_sparse_search_handles_chinese_and_english():
    tokens = tokenize_for_sparse_search("MonoLight 知识库 query_knowledge_base 20015")

    assert "monolight" in tokens
    assert "知识库" in tokens
    assert "query_knowledge_base" in tokens
    assert "20015" in tokens


def test_bm25_search_keyword_hit_ranked_first():
    chunks = [
        RetrievalChunk(id="1", content="向量数据库和语义检索", metadata={"source": "dense"}),
        RetrievalChunk(id="2", content="query_knowledge_base 工具用于查询知识库", metadata={"source": "tool"}),
    ]

    hits = bm25_search("query_knowledge_base", chunks, top_k=2)

    assert hits
    assert hits[0].id == "2"
    assert hits[0].sparse_rank == 1
    assert hits[0].sparse_score is not None


def test_reciprocal_rank_fusion_merges_dense_and_sparse_hits():
    dense_hits = [
        RetrievalHit(id="dense-only", content="dense", metadata={}, dense_distance=0.1, dense_rank=1),
        RetrievalHit(id="both", content="both", metadata={}, dense_distance=0.2, dense_rank=2),
    ]
    sparse_hits = [
        RetrievalHit(id="both", content="both", metadata={}, sparse_score=10.0, sparse_rank=1),
        RetrievalHit(id="sparse-only", content="sparse", metadata={}, sparse_score=5.0, sparse_rank=2),
    ]

    fused = reciprocal_rank_fusion(dense_hits, sparse_hits, top_k=3, rrf_k=60)

    assert fused[0].id == "both"
    assert fused[0].dense_rank == 2
    assert fused[0].sparse_rank == 1
    assert fused[0].fusion_score is not None


def test_parse_chroma_results():
    dense = parse_chroma_query_result(
        {
            "ids": [["chunk-1"]],
            "documents": [["content"]],
            "metadatas": [[{"filename": "a.md"}]],
            "distances": [[0.12]],
        }
    )
    chunks = parse_chroma_collection_items(
        {
            "ids": ["chunk-1"],
            "documents": ["content"],
            "metadatas": [{"filename": "a.md"}],
        }
    )

    assert dense[0].id == "chunk-1"
    assert dense[0].dense_rank == 1
    assert dense[0].dense_distance == 0.12
    assert chunks[0].id == "chunk-1"
    assert chunks[0].metadata["filename"] == "a.md"
