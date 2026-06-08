from rank_bm25 import BM25Okapi

from app.core.retrieval.schemas import RetrievalChunk, RetrievalHit
from app.core.retrieval.tokenizer import tokenize_for_sparse_search


def bm25_search(query: str, chunks: list[RetrievalChunk], top_k: int) -> list[RetrievalHit]:
    if not query or not chunks or top_k <= 0:
        return []

    tokenized_corpus = [tokenize_for_sparse_search(chunk.content) for chunk in chunks]
    if not any(tokenized_corpus):
        return []

    query_tokens = tokenize_for_sparse_search(query)
    if not query_tokens:
        return []

    bm25 = BM25Okapi(tokenized_corpus)
    scores = bm25.get_scores(query_tokens)
    query_token_set = set(query_tokens)
    ranked_indexes = sorted(
        range(len(chunks)),
        key=lambda index: (scores[index], len(query_token_set.intersection(tokenized_corpus[index]))),
        reverse=True,
    )

    hits: list[RetrievalHit] = []
    for rank, index in enumerate(ranked_indexes[:top_k], start=1):
        score = float(scores[index])
        if score <= 0 and not query_token_set.intersection(tokenized_corpus[index]):
            continue
        chunk = chunks[index]
        hits.append(
            RetrievalHit(
                id=chunk.id,
                content=chunk.content,
                metadata=chunk.metadata,
                sparse_score=score,
                sparse_rank=rank,
            )
        )

    return hits
