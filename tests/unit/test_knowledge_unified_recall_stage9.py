from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

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


def _profile(*, top_k: int = 5, candidate_k: int = 20, result_max_chars: int = 4000):
    return SimpleNamespace(
        id=9,
        uid="user-1",
        configs={
            "memory": {
                "knowledge": {
                    "top_k": top_k,
                    "candidate_k": candidate_k,
                    "result_max_chars": result_max_chars,
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
async def test_recall_document_lookup_supports_document_id_and_legacy_document_uuid() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=(KnowledgeBase.__table__, KnowledgeBaseDocument.__table__),
            )
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
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

    await engine.dispose()


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


def test_truncated_managed_result_drops_writable_identifiers() -> None:
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

    items = unified_recall._build_recall_items([(knowledge_base, hit)], top_k=1, result_max_chars=4)

    assert len(items) == 1
    assert items[0].content == "abcd"
    assert items[0].truncated is True
    assert items[0].knowledge_id is None
    assert items[0].knowledge_key is None
    assert items[0].knowledge_expected_version is None
    assert items[0].llm_maintainable is False


@pytest.mark.asyncio
async def test_list_recall_sources_only_returns_published_and_ready_profile_sources() -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = (
        KnowledgeBase.__table__,
        KnowledgeBaseCollectionOwner.__table__,
        KnowledgeBaseProfileBinding.__table__,
        KnowledgeBaseDocument.__table__,
        ManagedKnowledgeItem.__table__,
    )
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=tables))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
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

    await engine.dispose()
