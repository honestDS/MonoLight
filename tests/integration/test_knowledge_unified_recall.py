from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.pool import StaticPool

from app.core.crud.knowledge.base import knowledge_base_crud, knowledge_base_document_crud
from app.core.exceptions import RerankException
from app.core.knowledge import unified_recall
from app.core.knowledge.results import KnowledgeRecallSourceType
from app.core.retrieval.schemas import RetrievalHit
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseDocument,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseType,
    ManagedKnowledgeItem,
)


def _knowledge_base(
    knowledge_base_id: int,
    *,
    name: str,
    collection: str,
    signature: str,
    model_id: str = "embedding-model",
    knowledge_base_type: KnowledgeBaseType = KnowledgeBaseType.USER,
):
    return SimpleNamespace(
        id=knowledge_base_id,
        uid="user-1",
        name=name,
        knowledge_base_type=knowledge_base_type,
        index_status=KnowledgeBaseIndexStatus.READY,
        active_embedding_channel_id=1,
        active_embedding_model_id=model_id,
        active_embedding_dimensions=3,
        active_embedding_signature=signature,
        active_collection_name=collection,
        embedding_channel_id=1,
        embedding_model_id=model_id,
        embedding_dimensions=3,
        collection_name=collection,
    )


def _profile(*, top_k: int = 5, candidate_k: int = 20):
    return SimpleNamespace(
        id=9,
        uid="user-1",
        configs={
            "memory": {
                "knowledge": {
                    "top_k": top_k,
                    "candidate_k": candidate_k,
                }
            }
        },
    )


def _db():
    return SimpleNamespace(commit=AsyncMock())


@pytest.mark.asyncio
async def test_unified_recall_reuses_embedding_by_signature_and_supports_multiple_models(monkeypatch) -> None:
    knowledge_bases = [
        _knowledge_base(1, name="A", collection="a", signature="same"),
        _knowledge_base(2, name="B", collection="b", signature="same"),
        _knowledge_base(3, name="C", collection="c", signature="other", model_id="other-model"),
    ]
    embed_calls: list[tuple[int, str]] = []

    async def fake_list_sources(*_args, **_kwargs):
        return knowledge_bases

    async def fake_embed(_db, knowledge_base, _texts, _batch_size, **_kwargs):
        embed_calls.append((knowledge_base.id, knowledge_base.active_embedding_model_id))
        return [[float(knowledge_base.id), 0.0, 0.0]]

    async def fake_query(collection_name, _embedding, _query, limit, **_kwargs):
        return [RetrievalHit(id=f"{collection_name}-1", content=f"content-{collection_name}", metadata={"filename": f"{collection_name}.md", "chunk_index": 0})][:limit]

    async def fake_filter(_db, *, hits, **_kwargs):
        return hits

    async def no_rerank(*_args, **_kwargs):
        return None

    monkeypatch.setattr(unified_recall.knowledge_base_crud, "list_recall_sources_by_profile", fake_list_sources)
    monkeypatch.setattr(unified_recall, "embed_chunks_with_knowledge_base_config", fake_embed)
    monkeypatch.setattr(unified_recall, "hybrid_query_collection", fake_query)
    monkeypatch.setattr(unified_recall, "filter_recallable_managed_hits", fake_filter)
    monkeypatch.setattr(unified_recall, "get_profile_rerank_config", no_rerank)

    result = await unified_recall.knowledge_recall_service.recall(
        _db(),
        _profile(top_k=3, candidate_k=3),
        "topic",
    )

    assert len(embed_calls) == 2
    assert {model_id for _, model_id in embed_calls} == {"embedding-model", "other-model"}
    assert {item.knowledge_base_id for item in result.items} == {1, 2, 3}


@pytest.mark.asyncio
async def test_unified_recall_global_reranker_cross_sorts_sources_and_isolates_source_failure(monkeypatch) -> None:
    managed = _knowledge_base(1, name="Managed", collection="managed", signature="m", knowledge_base_type=KnowledgeBaseType.LLM_MANAGED)
    user = _knowledge_base(2, name="User", collection="user", signature="u")
    broken = _knowledge_base(3, name="Broken", collection="broken", signature="b")

    async def fake_list_sources(*_args, **_kwargs):
        return [managed, user, broken]

    async def fake_embed(_db, knowledge_base, _texts, _batch_size, **_kwargs):
        return [[float(knowledge_base.id), 0.0, 0.0]]

    async def fake_query(collection_name, _embedding, _query, limit, **_kwargs):
        if collection_name == "broken":
            raise RuntimeError("broken collection")
        if collection_name == "managed":
            return [RetrievalHit(id="managed-1", content="managed chunk", metadata={"knowledge_type": "managed", "managed_knowledge_id": 11, "managed_knowledge_version": 2, "chunk_index": 0})]
        return [RetrievalHit(id="user-1", content="user chunk", metadata={"knowledge_type": "user_document", "document_id": 21, "filename": "guide.md", "chunk_index": 0})]

    async def fake_filter(_db, *, hits, **_kwargs):
        return hits

    async def fake_rerank_config(*_args, **_kwargs):
        return SimpleNamespace(candidate_k=3, priority=1, channel_id=1, channel_name="rerank", model_id="rerank-model")

    async def fake_rerank(_config, _query, hits, _final_top_k):
        return list(reversed(hits))

    async def fake_materialize(_db, *, hits, **_kwargs):
        for hit in hits:
            hit.metadata.update({"managed_knowledge_key": "managed-key", "managed_knowledge_llm_maintainable": True})
            hit.content = "managed full content"
        return hits

    monkeypatch.setattr(unified_recall.knowledge_base_crud, "list_recall_sources_by_profile", fake_list_sources)
    monkeypatch.setattr(unified_recall, "embed_chunks_with_knowledge_base_config", fake_embed)
    monkeypatch.setattr(unified_recall, "hybrid_query_collection", fake_query)
    monkeypatch.setattr(unified_recall, "filter_recallable_managed_hits", fake_filter)
    monkeypatch.setattr(unified_recall, "get_profile_rerank_config", fake_rerank_config)
    monkeypatch.setattr(unified_recall, "rerank_retrieval_hits", fake_rerank)
    monkeypatch.setattr(unified_recall, "materialize_recallable_managed_hits", fake_materialize)

    result = await unified_recall.knowledge_recall_service.recall(_db(), _profile(top_k=2, candidate_k=3), "topic")

    assert [item.knowledge_base_id for item in result.items] == [2, 1]
    assert result.items[0].source_type == KnowledgeRecallSourceType.USER_KNOWLEDGE
    assert result.items[0].knowledge_id is None
    assert result.items[0].knowledge_expected_version is None
    assert result.items[0].llm_maintainable is False
    assert result.items[1].source_type == KnowledgeRecallSourceType.MANAGED_KNOWLEDGE
    assert result.items[1].knowledge_id == 11
    assert result.items[1].knowledge_expected_version == 2
    assert result.items[1].llm_maintainable is True
    assert not hasattr(result.items[0], "fusion_score")
    assert not hasattr(result.items[0], "rerank_score")


@pytest.mark.asyncio
async def test_unified_recall_rerank_failure_uses_cross_source_local_rank_fallback(monkeypatch) -> None:
    first = _knowledge_base(1, name="A", collection="a", signature="a")
    second = _knowledge_base(2, name="B", collection="b", signature="b")

    async def fake_list_sources(*_args, **_kwargs):
        return [first, second]

    async def fake_embed(_db, knowledge_base, _texts, _batch_size, **_kwargs):
        return [[float(knowledge_base.id), 0.0, 0.0]]

    async def fake_query(collection_name, _embedding, _query, _limit, **_kwargs):
        if collection_name == "a":
            return [
                RetrievalHit(id="a1", content="a1", metadata={"filename": "a.md", "chunk_index": 0}, fusion_score=-100.0),
                RetrievalHit(id="a2", content="a2", metadata={"filename": "a2.md", "chunk_index": 0}, fusion_score=999.0),
            ]
        return [
            RetrievalHit(id="b1", content="b1", metadata={"filename": "b.md", "chunk_index": 0}, fusion_score=500.0),
            RetrievalHit(id="b2", content="b2", metadata={"filename": "b2.md", "chunk_index": 0}, fusion_score=-999.0),
        ]

    async def fake_filter(_db, *, hits, **_kwargs):
        return hits

    async def fake_rerank_config(_db, _profile, excluded_priorities=None):
        if excluded_priorities:
            return None
        return SimpleNamespace(candidate_k=4, priority=1, channel_id=1, channel_name="rerank", model_id="rerank-model")

    async def failed_rerank(*_args, **_kwargs):
        raise RerankException("ERR_PROFILE_RERANK_CALL_FAILED", message_detail="failed")

    monkeypatch.setattr(unified_recall.knowledge_base_crud, "list_recall_sources_by_profile", fake_list_sources)
    monkeypatch.setattr(unified_recall, "embed_chunks_with_knowledge_base_config", fake_embed)
    monkeypatch.setattr(unified_recall, "hybrid_query_collection", fake_query)
    monkeypatch.setattr(unified_recall, "filter_recallable_managed_hits", fake_filter)
    monkeypatch.setattr(unified_recall, "get_profile_rerank_config", fake_rerank_config)
    monkeypatch.setattr(unified_recall, "rerank_retrieval_hits", failed_rerank)

    result = await unified_recall.knowledge_recall_service.recall(_db(), _profile(top_k=3, candidate_k=4), "topic")

    assert [item.content for item in result.items] == ["a1", "b1", "a2"]


def test_adjacent_user_document_chunks_are_merged_for_legacy_document_uuid() -> None:
    document = KnowledgeBaseDocument(
        id=21,
        knowledge_base_id=1,
        filename="legacy.md",
        content="hello world again",
        chunk_size=11,
        chunk_overlap=5,
        batch_size=16,
        chunk_count=2,
        chunk_ids=["chunk-0", "chunk-1"],
        metadata_={"document_uuid": "doc-1"},
    )
    hits = [
        RetrievalHit(id="chunk-0", content="hello world", metadata={"document_uuid": "doc-1", "filename": "legacy.md", "chunk_index": 0}),
        RetrievalHit(id="chunk-1", content="world again", metadata={"document_uuid": "doc-1", "filename": "legacy.md", "chunk_index": 1}),
        RetrievalHit(id="other", content="other", metadata={"document_uuid": "doc-2", "filename": "other.md", "chunk_index": 0}),
    ]

    merged = unified_recall._merge_adjacent_user_document_hits(
        hits,
        documents={("document_uuid", "doc-1"): document},
    )

    assert [hit.content for hit in merged] == ["hello world again", "other"]


def test_adjacent_user_document_chunks_restore_source_instead_of_guessing_overlap() -> None:
    document = KnowledgeBaseDocument(
        id=22,
        knowledge_base_id=1,
        filename="paragraphs.md",
        content="beta\n\napple",
        chunk_size=5,
        chunk_overlap=1,
        batch_size=16,
        chunk_count=2,
        chunk_ids=["chunk-0", "chunk-1"],
        metadata_={"document_uuid": "doc-2"},
    )
    hits = [
        RetrievalHit(id="chunk-0", content="beta", metadata={"document_uuid": "doc-2", "filename": "paragraphs.md", "chunk_index": 0}),
        RetrievalHit(id="chunk-1", content="apple", metadata={"document_uuid": "doc-2", "filename": "paragraphs.md", "chunk_index": 1}),
    ]

    merged = unified_recall._merge_adjacent_user_document_hits(
        hits,
        documents={("document_uuid", "doc-2"): document},
    )

    assert [hit.content for hit in merged] == ["beta\n\napple"]


@pytest.mark.asyncio
async def test_recall_document_lookup_supports_document_id_and_legacy_document_uuid(database_factory) -> None:
    async with database_factory(
        tables=(KnowledgeBase.__table__, KnowledgeBaseDocument.__table__),
        poolclass=StaticPool,
    ) as session_factory:
        async with session_factory() as session:
            knowledge_base = KnowledgeBase(
                uid="user-1",
                name="user-kb",
                embedding_channel_id=1,
                embedding_model_id="embedding-model",
                embedding_dimensions=3,
                collection_name="user-kb",
            )
            session.add(knowledge_base)
            await session.flush()
            current = KnowledgeBaseDocument(
                knowledge_base_id=knowledge_base.id,
                filename="current.md",
                content="current",
                chunk_size=100,
                chunk_overlap=10,
                batch_size=16,
                chunk_count=1,
                chunk_ids=["current-0"],
                metadata_={"document_uuid": "current-uuid"},
            )
            legacy = KnowledgeBaseDocument(
                knowledge_base_id=knowledge_base.id,
                filename="legacy.md",
                content="legacy",
                chunk_size=100,
                chunk_overlap=10,
                batch_size=16,
                chunk_count=1,
                chunk_ids=["legacy-0"],
                metadata_={"document_uuid": "legacy-uuid"},
            )
            session.add_all([current, legacy])
            await session.commit()

            documents = await knowledge_base_document_crud.list_by_recall_references(
                session,
                knowledge_base_id=knowledge_base.id,
                document_ids=[current.id],
                document_uuids=["legacy-uuid"],
            )

            assert {document.id for document in documents} == {current.id, legacy.id}


@pytest.mark.asyncio
async def test_unified_recall_refills_after_stale_managed_candidates(monkeypatch) -> None:
    managed = _knowledge_base(1, name="Managed", collection="managed", signature="m", knowledge_base_type=KnowledgeBaseType.LLM_MANAGED)
    all_hits = [
        RetrievalHit(id="stale-1", content="stale", metadata={"knowledge_type": "managed"}),
        RetrievalHit(id="stale-2", content="stale", metadata={"knowledge_type": "managed"}),
        RetrievalHit(id="stale-3", content="stale", metadata={"knowledge_type": "managed"}),
        RetrievalHit(id="valid-1", content="valid one", metadata={"knowledge_type": "managed", "managed_knowledge_id": 11, "managed_knowledge_version": 1}),
        RetrievalHit(id="valid-2", content="valid two", metadata={"knowledge_type": "managed", "managed_knowledge_id": 12, "managed_knowledge_version": 1}),
    ]
    requested_limits: list[int] = []

    async def fake_list_sources(*_args, **_kwargs):
        return [managed]

    async def fake_embed(*_args, **_kwargs):
        return [[0.1, 0.2, 0.3]]

    async def fake_query(_collection, _embedding, _query, limit, **_kwargs):
        requested_limits.append(limit)
        return all_hits[:limit]

    async def fake_filter(_db, *, hits, **_kwargs):
        return [hit for hit in hits if not hit.id.startswith("stale-")]

    async def no_rerank(*_args, **_kwargs):
        return None

    async def fake_materialize(_db, *, hits, **_kwargs):
        for hit in hits:
            hit.metadata.update({"managed_knowledge_key": hit.id, "managed_knowledge_llm_maintainable": True})
        return hits

    monkeypatch.setattr(unified_recall.knowledge_base_crud, "list_recall_sources_by_profile", fake_list_sources)
    monkeypatch.setattr(unified_recall, "embed_chunks_with_knowledge_base_config", fake_embed)
    monkeypatch.setattr(unified_recall, "hybrid_query_collection", fake_query)
    monkeypatch.setattr(unified_recall, "filter_recallable_managed_hits", fake_filter)
    monkeypatch.setattr(unified_recall, "get_profile_rerank_config", no_rerank)
    monkeypatch.setattr(unified_recall, "materialize_recallable_managed_hits", fake_materialize)

    result = await unified_recall.knowledge_recall_service.recall(_db(), _profile(top_k=2, candidate_k=2), "topic")

    assert requested_limits == [2, 4, 8]
    assert [item.content for item in result.items] == ["valid one", "valid two"]


@pytest.mark.asyncio
async def test_unified_recall_does_not_expand_for_adjacent_user_chunks(monkeypatch) -> None:
    user = _knowledge_base(1, name="User", collection="user", signature="u")
    document = KnowledgeBaseDocument(
        id=21,
        knowledge_base_id=1,
        filename="guide.md",
        content="abcdefghij",
        chunk_size=5,
        chunk_overlap=1,
        batch_size=16,
        chunk_count=3,
        chunk_ids=["chunk-0", "chunk-1", "chunk-2"],
        metadata_={"document_uuid": "doc-1"},
    )
    all_hits = [
        RetrievalHit(
            id="chunk-0",
            content="abcde",
            metadata={"knowledge_type": "user_document", "document_id": 21, "filename": "guide.md", "chunk_index": 0},
        ),
        RetrievalHit(
            id="chunk-1",
            content="efghi",
            metadata={"knowledge_type": "user_document", "document_id": 21, "filename": "guide.md", "chunk_index": 1},
        ),
        RetrievalHit(
            id="chunk-2",
            content="ij",
            metadata={"knowledge_type": "user_document", "document_id": 21, "filename": "guide.md", "chunk_index": 2},
        ),
    ]
    requested_limits: list[int] = []

    async def fake_list_sources(*_args, **_kwargs):
        return [user]

    async def fake_embed(*_args, **_kwargs):
        return [[0.1, 0.2, 0.3]]

    async def fake_query(_collection, _embedding, _query, limit, **_kwargs):
        requested_limits.append(limit)
        return all_hits[:limit]

    async def fake_filter(_db, *, hits, **_kwargs):
        return hits

    async def fake_documents(*_args, **_kwargs):
        return [document]

    async def no_rerank(*_args, **_kwargs):
        return None

    monkeypatch.setattr(unified_recall.knowledge_base_crud, "list_recall_sources_by_profile", fake_list_sources)
    monkeypatch.setattr(unified_recall, "embed_chunks_with_knowledge_base_config", fake_embed)
    monkeypatch.setattr(unified_recall, "hybrid_query_collection", fake_query)
    monkeypatch.setattr(unified_recall, "filter_recallable_managed_hits", fake_filter)
    monkeypatch.setattr(unified_recall.knowledge_base_document_crud, "list_by_recall_references", fake_documents)
    monkeypatch.setattr(unified_recall, "get_profile_rerank_config", no_rerank)

    result = await unified_recall.knowledge_recall_service.recall(_db(), _profile(top_k=2, candidate_k=2), "topic")

    assert requested_limits == [2]
    assert [item.content for item in result.items] == ["abcdefghi"]


def test_managed_result_preserves_full_content_and_writable_identifiers() -> None:
    knowledge_base = _knowledge_base(1, name="Managed", collection="managed", signature="m", knowledge_base_type=KnowledgeBaseType.LLM_MANAGED)
    hit = RetrievalHit(
        id="managed-1",
        content="abcdefgh",
        metadata={
            "knowledge_type": "managed",
            "managed_knowledge_id": 11,
            "managed_knowledge_version": 2,
            "managed_knowledge_key": "key-1",
            "managed_knowledge_llm_maintainable": True,
        },
    )

    items = unified_recall._build_recall_items([(knowledge_base, hit)], top_k=1)

    assert len(items) == 1
    assert items[0].content == "abcdefgh"
    assert items[0].truncated is False
    assert items[0].knowledge_id == 11
    assert items[0].knowledge_key == "key-1"
    assert items[0].knowledge_expected_version == 2
    assert items[0].llm_maintainable is True


@pytest.mark.parametrize(
    ("source_order", "top_k", "expected"),
    [
        (
            ("managed-before", "user", "managed-after"),
            3,
            [
                ("managed-before", "managed full before", False),
                ("guide.md", "abcdefgh", False),
                ("managed-after", "managed full after", False),
            ],
        ),
        (
            ("user", "managed-before"),
            3,
            [("guide.md", "abcdefgh", False), ("managed-before", "managed full before", False)],
        ),
        (
            ("managed-before", "user", "managed-after"),
            2,
            [("managed-before", "managed full before", False), ("guide.md", "abcdefgh", False)],
        ),
    ],
)
def test_mixed_source_results_preserve_full_content_and_respect_top_k(source_order, top_k, expected) -> None:
    managed = _knowledge_base(1, name="Managed", collection="managed", signature="m", knowledge_base_type=KnowledgeBaseType.LLM_MANAGED)
    user = _knowledge_base(2, name="User", collection="user", signature="u")

    def managed_hit(knowledge_id: int, key: str, content: str) -> RetrievalHit:
        return RetrievalHit(
            id=f"managed-{knowledge_id}",
            content=content,
            metadata={
                "knowledge_type": "managed",
                "managed_knowledge_id": knowledge_id,
                "managed_knowledge_version": 2,
                "managed_knowledge_key": key,
                "managed_knowledge_llm_maintainable": True,
            },
        )

    sources = {
        "managed-before": (managed, managed_hit(11, "managed-before", "managed full before")),
        "user": (
            user,
            RetrievalHit(
                id="user-1",
                content="abcdefgh",
                metadata={"knowledge_type": "user_document", "filename": "guide.md", "document_id": 21},
            ),
        ),
        "managed-after": (managed, managed_hit(12, "managed-after", "managed full after")),
    }

    items = unified_recall._build_recall_items(
        [sources[name] for name in source_order],
        top_k=top_k,
    )

    assert [(item.source, item.content, item.truncated) for item in items] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "knowledge_base_type",
    [KnowledgeBaseType.LLM_MANAGED, KnowledgeBaseType.USER],
    ids=["managed", "user"],
)
async def test_unified_recall_returns_full_selected_content_from_database(knowledge_base_type, database_factory, monkeypatch) -> None:
    async with database_factory(
        tables=(
            KnowledgeBase.__table__,
            KnowledgeBaseCollectionOwner.__table__,
            KnowledgeBaseProfileBinding.__table__,
            KnowledgeBaseDocument.__table__,
            ManagedKnowledgeItem.__table__,
        ),
        poolclass=StaticPool,
    ) as session_factory:
        async with session_factory() as session:
            is_managed = knowledge_base_type == KnowledgeBaseType.LLM_MANAGED
            knowledge_base = KnowledgeBase(
                uid="user-1",
                name="managed-kb" if is_managed else "user-kb",
                embedding_channel_id=1,
                embedding_model_id="embedding-model",
                embedding_dimensions=3,
                collection_name="managed-kb" if is_managed else "user-kb",
                knowledge_base_type=knowledge_base_type,
                managed_profile_id=9 if is_managed else None,
                active_embedding_channel_id=1,
                active_embedding_model_id="embedding-model",
                active_embedding_dimensions=3,
                active_embedding_signature="sig-managed" if is_managed else "sig-user",
                active_embedding_revision=1,
                active_collection_name="managed-kb-active" if is_managed else "user-kb-active",
                index_status=KnowledgeBaseIndexStatus.READY,
            )
            session.add(knowledge_base)
            await session.flush()
            knowledge_base_id = knowledge_base.id

            tail_marker = "UNIQUE_MANAGED_RECALL_TAIL_9f17c2" if is_managed else "UNIQUE_USER_RECALL_TAIL_9f17c2"
            document_content = None
            nonmatching_tail_marker = None
            if is_managed:
                content = ("Full managed knowledge body. " * 180) + tail_marker
                session.add(
                    ManagedKnowledgeItem(
                        id=91,
                        uid="user-1",
                        knowledge_base_id=knowledge_base.id,
                        knowledge_key="integration-managed-key",
                        content=content,
                        content_token_count=1800,
                        content_hash="c" * 64,
                        version=4,
                        indexed_version=4,
                        vector_item_ids=["managed-hit-91"],
                        is_recallable=True,
                        llm_maintainable=True,
                    )
                )
                hit = RetrievalHit(
                    id="managed-hit-91",
                    content="vector chunk only",
                    metadata={
                        "knowledge_type": "managed",
                        "managed_knowledge_id": 91,
                        "managed_knowledge_version": 4,
                    },
                )
            else:
                content = ("Selected matching user retrieval chunk. " * 150) + tail_marker
                nonmatching_tail_marker = "UNIQUE_USER_DOCUMENT_ONLY_TAIL_9f17c2"
                document_content = ("Full user document body not selected by retrieval. " * 100) + nonmatching_tail_marker
                filename = "integration-user.md"
                document = KnowledgeBaseDocument(
                    knowledge_base_id=knowledge_base.id,
                    filename=filename,
                    content=document_content,
                    chunk_size=6000,
                    chunk_overlap=100,
                    batch_size=16,
                    chunk_count=2,
                    chunk_ids=["user-hit-91", "user-nonmatching-92"],
                    metadata_={"document_uuid": "integration-user-document"},
                )
                session.add(document)
                await session.flush()
                session.add(
                    KnowledgeBaseProfileBinding(
                        uid="user-1",
                        knowledge_base_id=knowledge_base.id,
                        profile_id=9,
                    )
                )
                hit = RetrievalHit(
                    id="user-hit-91",
                    content=content,
                    metadata={
                        "knowledge_type": "user_document",
                        "document_id": document.id,
                        "filename": filename,
                        "chunk_index": 0,
                    },
                )
            await session.commit()

            async def fake_embed(*_args, **_kwargs):
                return [[0.1, 0.2, 0.3]]

            async def fake_query(_collection, _embedding, _query, limit, **_kwargs):
                return [hit][:limit]

            async def no_rerank(*_args, **_kwargs):
                return None

            monkeypatch.setattr(unified_recall, "embed_chunks_with_knowledge_base_config", fake_embed)
            monkeypatch.setattr(unified_recall, "hybrid_query_collection", fake_query)
            monkeypatch.setattr(unified_recall, "get_profile_rerank_config", no_rerank)

            profile = _profile(top_k=1, candidate_k=1)
            profile.configs["memory"]["knowledge"]["result_max_chars"] = 256
            result = await unified_recall.knowledge_recall_service.recall(session, profile, "topic")

    assert len(content) > 4000
    assert len(result.items) == 1
    item = result.items[0]
    assert item.content == content
    assert item.content.endswith(tail_marker)
    assert item.knowledge_base_id == knowledge_base_id
    assert item.truncated is False
    if knowledge_base_type == KnowledgeBaseType.LLM_MANAGED:
        assert item.source_type == KnowledgeRecallSourceType.MANAGED_KNOWLEDGE
        assert item.knowledge_id == 91
        assert item.knowledge_key == "integration-managed-key"
        assert item.source == "integration-managed-key"
        assert item.knowledge_expected_version == 4
        assert item.llm_maintainable is True
    else:
        assert item.source_type == KnowledgeRecallSourceType.USER_KNOWLEDGE
        assert item.source == "integration-user.md"
        assert item.knowledge_id is None
        assert item.knowledge_expected_version is None
        assert item.llm_maintainable is False
        assert item.content != document_content
        assert nonmatching_tail_marker not in item.content


@pytest.mark.asyncio
async def test_list_recall_sources_only_returns_published_and_ready_profile_sources(database_factory) -> None:
    async with database_factory(
        tables=(
            KnowledgeBase.__table__,
            KnowledgeBaseCollectionOwner.__table__,
            KnowledgeBaseProfileBinding.__table__,
            KnowledgeBaseDocument.__table__,
            ManagedKnowledgeItem.__table__,
        ),
        poolclass=StaticPool,
    ) as session_factory:
        async with session_factory() as session:
            user_ready = KnowledgeBase(
                uid="user-1",
                name="user-ready",
                embedding_channel_id=1,
                embedding_model_id="embedding-model",
                embedding_dimensions=3,
                collection_name="user-ready",
                active_embedding_channel_id=1,
                active_embedding_model_id="embedding-model",
                active_embedding_dimensions=3,
                active_embedding_signature="sig-user-ready",
                active_embedding_revision=1,
                active_collection_name="user-ready-active",
                index_status=KnowledgeBaseIndexStatus.READY,
            )
            user_empty = KnowledgeBase(
                uid="user-1",
                name="user-empty",
                embedding_channel_id=1,
                embedding_model_id="embedding-model",
                embedding_dimensions=3,
                collection_name="user-empty",
                active_embedding_channel_id=1,
                active_embedding_model_id="embedding-model",
                active_embedding_dimensions=3,
                active_embedding_signature="sig-user-empty",
                active_embedding_revision=1,
                active_collection_name="user-empty-active",
                index_status=KnowledgeBaseIndexStatus.READY,
            )
            user_failed = KnowledgeBase(
                uid="user-1",
                name="user-failed",
                embedding_channel_id=1,
                embedding_model_id="embedding-model",
                embedding_dimensions=3,
                collection_name="user-failed",
                active_embedding_channel_id=1,
                active_embedding_model_id="embedding-model",
                active_embedding_dimensions=3,
                active_embedding_signature="sig-user-failed",
                active_embedding_revision=1,
                active_collection_name="user-failed-active",
                index_status=KnowledgeBaseIndexStatus.FAILED,
            )
            managed_ready = KnowledgeBase(
                uid="user-1",
                name="managed-ready",
                embedding_channel_id=1,
                embedding_model_id="embedding-model",
                embedding_dimensions=3,
                collection_name="managed-ready",
                knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
                managed_profile_id=9,
                active_embedding_channel_id=1,
                active_embedding_model_id="embedding-model",
                active_embedding_dimensions=3,
                active_embedding_signature="sig-managed-ready",
                active_embedding_revision=1,
                active_collection_name="managed-ready-active",
                index_status=KnowledgeBaseIndexStatus.READY,
            )
            managed_stale = KnowledgeBase(
                uid="user-1",
                name="managed-stale",
                embedding_channel_id=1,
                embedding_model_id="embedding-model",
                embedding_dimensions=3,
                collection_name="managed-stale",
                knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
                managed_profile_id=10,
                active_embedding_channel_id=1,
                active_embedding_model_id="embedding-model",
                active_embedding_dimensions=3,
                active_embedding_signature="sig-managed-stale",
                active_embedding_revision=1,
                active_collection_name="managed-stale-active",
                index_status=KnowledgeBaseIndexStatus.READY,
            )
            session.add_all([user_ready, user_empty, user_failed, managed_ready, managed_stale])
            await session.flush()

            session.add_all(
                [
                    KnowledgeBaseProfileBinding(uid="user-1", knowledge_base_id=user_ready.id, profile_id=9),
                    KnowledgeBaseProfileBinding(uid="user-1", knowledge_base_id=user_empty.id, profile_id=9),
                    KnowledgeBaseProfileBinding(uid="user-1", knowledge_base_id=user_failed.id, profile_id=9),
                    KnowledgeBaseDocument(
                        knowledge_base_id=user_ready.id,
                        filename="ready.md",
                        content="ready",
                        chunk_size=1000,
                        chunk_overlap=100,
                        batch_size=16,
                        chunk_count=1,
                        chunk_ids=["ready-1"],
                    ),
                    KnowledgeBaseDocument(
                        knowledge_base_id=user_failed.id,
                        filename="failed.md",
                        content="failed",
                        chunk_size=1000,
                        chunk_overlap=100,
                        batch_size=16,
                        chunk_count=1,
                        chunk_ids=["failed-1"],
                    ),
                    ManagedKnowledgeItem(
                        uid="user-1",
                        knowledge_base_id=managed_ready.id,
                        knowledge_key="ready-key",
                        content="managed ready",
                        content_token_count=2,
                        content_hash="a" * 64,
                        version=2,
                        indexed_version=2,
                        vector_item_ids=["managed-ready-1"],
                        is_recallable=True,
                    ),
                    ManagedKnowledgeItem(
                        uid="user-1",
                        knowledge_base_id=managed_stale.id,
                        knowledge_key="stale-key",
                        content="managed stale",
                        content_token_count=2,
                        content_hash="b" * 64,
                        version=2,
                        indexed_version=1,
                        vector_item_ids=["managed-stale-1"],
                        is_recallable=True,
                    ),
                ]
            )
            await session.commit()

            sources = await knowledge_base_crud.list_recall_sources_by_profile(session, uid="user-1", profile_id=9)
            stale_sources = await knowledge_base_crud.list_recall_sources_by_profile(session, uid="user-1", profile_id=10)

            assert [source.name for source in sources] == ["user-ready", "managed-ready"]
            assert stale_sources == []
