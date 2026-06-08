from app.core.retrieval.schemas import RetrievalHit


def reciprocal_rank_fusion(
    dense_results: list[RetrievalHit],
    sparse_results: list[RetrievalHit],
    top_k: int,
    rrf_k: int = 60,
) -> list[RetrievalHit]:
    if top_k <= 0:
        return []

    merged: dict[str, RetrievalHit] = {}
    scores: dict[str, float] = {}

    def merge_hit(hit: RetrievalHit, rank: int, source: str):
        existing = merged.get(hit.id)
        if existing is None:
            existing = RetrievalHit(id=hit.id, content=hit.content, metadata=hit.metadata)
            merged[hit.id] = existing

        if source == "dense":
            existing.dense_distance = hit.dense_distance
            existing.dense_rank = hit.dense_rank or rank
        else:
            existing.sparse_score = hit.sparse_score
            existing.sparse_rank = hit.sparse_rank or rank

        scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (rrf_k + rank)

    for rank, hit in enumerate(dense_results, start=1):
        merge_hit(hit, rank, "dense")

    for rank, hit in enumerate(sparse_results, start=1):
        merge_hit(hit, rank, "sparse")

    for hit_id, score in scores.items():
        merged[hit_id].fusion_score = score

    return sorted(merged.values(), key=lambda hit: hit.fusion_score or 0.0, reverse=True)[:top_k]
