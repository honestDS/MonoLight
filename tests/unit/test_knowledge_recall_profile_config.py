from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.embedding import knowledge_base as embedding_knowledge_base
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.rerank import knowledge_base as rerank_knowledge_base
from app.core.retrieval.schemas import RetrievalHit


def test_get_profile_kb_query_top_k_reads_explicit_knowledge_config() -> None:
    profile = SimpleNamespace(
        configs={
            "memory": {
                "knowledge": {
                    "top_k": 13,
                }
            }
        }
    )

    assert embedding_knowledge_base.get_profile_kb_query_top_k(profile) == 13


def test_get_profile_kb_query_top_k_supports_legacy_rerank_config() -> None:
    legacy_top_k = 9
    profile = SimpleNamespace(
        configs={
            "channel": {
                "rerank_channel": {
                    "kb_query_top_k": legacy_top_k,
                }
            }
        }
    )

    assert embedding_knowledge_base.get_profile_kb_query_top_k(profile) == legacy_top_k


@pytest.mark.parametrize(
    "configs",
    [
        {"memory": {"knowledge": {"top_k": 0}}},
        {"memory": {"knowledge": {"top_k": "not-a-number"}}},
    ],
)
def test_get_profile_kb_query_top_k_falls_back_for_invalid_profile_config(configs: dict) -> None:
    profile = SimpleNamespace(configs=configs)

    assert embedding_knowledge_base.get_profile_kb_query_top_k(profile) == embedding_knowledge_base.KNOWLEDGE_BASE_QUERY_TOP_K


def test_active_knowledge_base_embedding_snapshot_overrides_legacy_fields() -> None:
    knowledge_base = SimpleNamespace(
        embedding_channel_id=1,
        embedding_model_id="legacy-model",
        embedding_dimensions=3,
        collection_name="legacy-collection",
        active_embedding_channel_id=2,
        active_embedding_model_id="active-model",
        active_embedding_dimensions=4,
        active_collection_name="active-collection",
    )

    snapshot = resolve_active_knowledge_base_embedding(knowledge_base)

    assert snapshot.channel_id == 2
    assert snapshot.model_id == "active-model"
    assert snapshot.dimensions == 4
    assert snapshot.collection_name == "active-collection"


@pytest.mark.asyncio
async def test_get_profile_rerank_config_uses_knowledge_candidate_k(monkeypatch) -> None:
    channel = SimpleNamespace(
        id=17,
        name="rerank-channel",
        base_url="https://rerank.invalid",
        http_proxy=None,
        get_decrypted_api_key=lambda: "api-key",
    )
    model_entry = {
        "model_id": "rerank-model",
        "usage": "RERANK",
        "protocol": "COHERE_RERANK",
    }
    rerank_channel = {
        "rerank_candidate_k": 49,
        "rules": [
            {
                "channel_id": 17,
                "model_id": "rerank-model",
                "priority": 2,
                "weight": 100,
            }
        ],
    }
    profile = SimpleNamespace(
        id=23,
        configs={
            "channel": {"rerank_channel": rerank_channel},
            "memory": {
                "knowledge": {
                    "top_k": 8,
                    "candidate_k": 31,
                    "result_max_chars": 4000,
                }
            },
        },
    )

    async def fake_select_channel(_db, channel_config, expected_usage, **_kwargs):
        assert expected_usage == "RERANK"
        return channel, model_entry, channel_config.rules[0]

    monkeypatch.setattr(rerank_knowledge_base, "select_channel", fake_select_channel)

    result = await rerank_knowledge_base.get_profile_rerank_config(object(), profile)

    assert result is not None
    assert result.candidate_k == 31


@pytest.mark.asyncio
async def test_query_recallable_candidates_refills_after_stale_managed_hits(monkeypatch) -> None:
    all_hits = [
        RetrievalHit(id="stale-1", content="stale", metadata={"knowledge_type": "managed"}),
        RetrievalHit(id="stale-2", content="stale", metadata={"knowledge_type": "managed"}),
        RetrievalHit(id="stale-3", content="stale", metadata={"knowledge_type": "managed"}),
        RetrievalHit(id="valid-1", content="valid", metadata={}),
        RetrievalHit(id="valid-2", content="valid", metadata={}),
    ]
    requested_limits: list[int] = []
    log_messages: list[str] = []

    async def fake_query(_collection_name, _embedding, _query, limit, **_kwargs):
        requested_limits.append(limit)
        return all_hits[:limit]

    async def fake_filter(_db, *, hits, **_kwargs):
        return [hit for hit in hits if not hit.id.startswith("stale-")]

    class _BoundLogger:
        def info(self, message: str) -> None:
            log_messages.append(message)

    class _Logger:
        def bind(self, **_kwargs):
            return _BoundLogger()

    db = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(embedding_knowledge_base, "hybrid_query_collection", fake_query)
    monkeypatch.setattr(embedding_knowledge_base, "filter_recallable_managed_hits", fake_filter)
    monkeypatch.setattr(embedding_knowledge_base, "logger", _Logger())

    result = await embedding_knowledge_base._query_recallable_candidates(
        db,
        profile_uid="user-1",
        knowledge_base_id=1,
        collection_name="collection",
        query_embedding=[0.1, 0.2],
        query="test",
        target_count=2,
    )

    assert [hit.id for hit in result] == ["valid-1", "valid-2"]
    assert requested_limits == [2, 4, 8]
    assert len(log_messages) == 2
    assert "2 -> 4" in log_messages[0]
    assert "4 -> 8" in log_messages[1]


@pytest.mark.asyncio
async def test_query_reranks_managed_chunks_before_materializing_full_content(monkeypatch) -> None:
    knowledge_base = SimpleNamespace(
        id=7,
        uid="user-1",
        active_embedding_channel_id=1,
        active_embedding_model_id="embedding-model",
        active_embedding_dimensions=3,
        active_embedding_revision=1,
        active_collection_name="managed-collection",
        embedding_channel_id=1,
        embedding_model_id="embedding-model",
        embedding_dimensions=3,
        collection_name="managed-collection",
    )
    profile = SimpleNamespace(id=9, uid="user-1", configs={})
    db = SimpleNamespace(get=AsyncMock(return_value=knowledge_base))
    candidate_hits = [
        RetrievalHit(
            id="managed-chunk-0",
            content="partial managed chunk",
            metadata={
                "knowledge_type": "managed",
                "managed_knowledge_id": 31,
                "managed_knowledge_version": 4,
            },
            fusion_score=0.9,
        ),
        RetrievalHit(
            id="user-document",
            content="user document chunk",
            metadata={"filename": "manual.md"},
            fusion_score=0.8,
        ),
    ]

    async def fake_embed(*_args, **_kwargs):
        return [[0.1, 0.2, 0.3]]

    async def fake_rerank_config(*_args, **_kwargs):
        return SimpleNamespace(
            candidate_k=2,
            priority=1,
            channel_id=2,
            channel_name="rerank",
            model_id="rerank-model",
        )

    async def fake_candidates(*_args, **_kwargs):
        return candidate_hits

    async def fake_rerank(_config, _query, hits, _final_top_k):
        assert [hit.content for hit in hits] == [
            "partial managed chunk",
            "user document chunk",
        ]
        return hits

    async def fake_materialize(_db, *, hits, **_kwargs):
        assert _db is db
        assert hits[0].content == "partial managed chunk"
        return [
            RetrievalHit(
                id=hits[0].id,
                content="complete managed relation content",
                metadata={
                    **hits[0].metadata,
                    "managed_knowledge_llm_maintainable": True,
                },
                fusion_score=hits[0].fusion_score,
            )
        ]

    def fake_build_response(hits, **_kwargs):
        return SimpleNamespace(items=hits)

    monkeypatch.setattr(
        embedding_knowledge_base,
        "embed_chunks_with_knowledge_base_config",
        fake_embed,
    )
    monkeypatch.setattr(
        embedding_knowledge_base,
        "get_profile_rerank_config",
        fake_rerank_config,
    )
    monkeypatch.setattr(
        embedding_knowledge_base,
        "_query_recallable_candidates",
        fake_candidates,
    )
    monkeypatch.setattr(
        embedding_knowledge_base,
        "rerank_retrieval_hits",
        fake_rerank,
    )
    monkeypatch.setattr(
        embedding_knowledge_base,
        "materialize_recallable_managed_hits",
        fake_materialize,
    )
    monkeypatch.setattr(
        embedding_knowledge_base,
        "build_query_test_response",
        fake_build_response,
    )

    result = await embedding_knowledge_base.query_knowledge_base(
        db,
        profile,
        7,
        "managed topic",
        top_k=1,
        require_binding=False,
    )

    assert result.items[0].content == "complete managed relation content"


@pytest.mark.asyncio
async def test_final_query_response_materializes_before_top_k(monkeypatch) -> None:
    hits = [
        RetrievalHit(id="stale-first", content="stale", metadata={}),
        RetrievalHit(id="valid-second", content="second", metadata={}),
        RetrievalHit(id="valid-third", content="third", metadata={}),
    ]

    async def fake_materialize(_db, *, hits, **_kwargs):
        assert [hit.id for hit in hits] == [
            "stale-first",
            "valid-second",
            "valid-third",
        ]
        return hits[1:]

    def fake_build_response(hits, **_kwargs):
        return SimpleNamespace(items=hits)

    monkeypatch.setattr(
        embedding_knowledge_base,
        "materialize_recallable_managed_hits",
        fake_materialize,
    )
    monkeypatch.setattr(
        embedding_knowledge_base,
        "build_query_test_response",
        fake_build_response,
    )

    result = await embedding_knowledge_base._build_final_query_response(
        SimpleNamespace(),
        profile_uid="user-1",
        knowledge_base_id=7,
        hits=hits,
        final_top_k=2,
        retrieval_mode="hybrid",
    )

    assert [hit.id for hit in result.items] == ["valid-second", "valid-third"]
