import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.core.knowledge.documents as knowledge_documents
from app.api.v1 import knowledge_base as knowledge_base_api
from app.core.constants import (
    ERR_KB_CHUNK_COUNT_EXCEEDED,
    ERR_KB_CHUNK_TOTAL_CHARS_EXCEEDED,
    ERR_KB_CONTENT_TOO_LONG,
    ERR_KB_FILE_TOO_LARGE,
    ERR_KB_REQUEST_TOO_LARGE,
    KNOWLEDGE_CONTENT_MAX_BYTES,
    KNOWLEDGE_CONTENT_MAX_CHARS,
    KNOWLEDGE_DOCUMENT_MAX_CHUNKS,
    KNOWLEDGE_DOCUMENT_MAX_TOTAL_CHARS,
    KNOWLEDGE_FILE_MAX_BYTES,
    KNOWLEDGE_REQUEST_MAX_BYTES,
    MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS,
)
from app.core.i18n import t
from app.core.security import get_current_user
from app.core.utils.tokenizer import estimate_tokens
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseDocument,
    KnowledgeBaseEmbeddingDelta,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseMigrationStatus,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.user import User
from app.providers.database import get_db

_ENTRY_CREATE_PATH = "/api/v1/knowledge-base/entries/create"
_ENTRY_PREVIEW_PATH = "/api/v1/knowledge-base/entries/preview-file"
_TABLES = (
    User.__table__,
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    KnowledgeBaseCollectionOwner.__table__,
    KnowledgeBaseDocument.__table__,
    KnowledgeBaseProfileBinding.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeJob.__table__,
    KnowledgeBaseEmbeddingDelta.__table__,
)


@pytest_asyncio.fixture
async def db_session(database_factory) -> AsyncIterator[AsyncSession]:
    async with database_factory(tables=_TABLES, foreign_keys=True) as session_factory:
        async with session_factory() as session:
            yield session


@pytest.fixture
def vector_backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, dict[str, Any]]]:
    vectors: dict[str, dict[str, dict[str, Any]]] = {}

    async def fake_embed_chunks(
        _db: AsyncSession,
        knowledge_base: KnowledgeBase,
        chunks: list[str],
        _batch_size: int,
        **_kwargs: Any,
    ) -> list[list[float]]:
        dimensions = knowledge_base.active_embedding_dimensions or knowledge_base.embedding_dimensions or 1
        return [[0.01] * dimensions for _ in chunks]

    async def fake_get_or_create_collection(collection_name: str, **_kwargs: Any) -> dict[str, dict[str, Any]]:
        return vectors.setdefault(collection_name, {})

    async def fake_upsert(
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> int:
        collection = vectors.setdefault(collection_name, {})
        for item_id, document, embedding, metadata in zip(item_ids, documents, embeddings, metadatas, strict=True):
            collection[item_id] = {
                "document": document,
                "embedding": embedding,
                "metadata": metadata,
            }
        return len(item_ids)

    async def fake_delete(collection_name: str, item_ids: list[str], **_kwargs: Any) -> int:
        collection = vectors.setdefault(collection_name, {})
        for item_id in item_ids:
            collection.pop(item_id, None)
        return len(item_ids)

    monkeypatch.setattr(knowledge_documents, "embed_chunks_with_knowledge_base_config", fake_embed_chunks)
    monkeypatch.setattr(knowledge_documents, "async_get_or_create_collection", fake_get_or_create_collection)
    monkeypatch.setattr(knowledge_documents, "async_upsert_collection_items", fake_upsert)
    monkeypatch.setattr(knowledge_documents, "async_delete_collection_items", fake_delete)
    return vectors


@pytest.fixture
def knowledge_base_app(db_session: AsyncSession) -> FastAPI:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_get_current_user() -> User:
        return User(uid="owner-a", username="owner-a", is_superuser=False)

    app = FastAPI()
    register_handlers(app)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.include_router(knowledge_base_api.router, prefix="/api/v1")
    return app


@pytest_asyncio.fixture
async def api_client(knowledge_base_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=knowledge_base_app), base_url="http://test") as client:
        yield client


def _make_knowledge_base(
    *,
    uid: str,
    name: str,
    collection_name: str,
    channel_id: int,
    knowledge_base_type: KnowledgeBaseType,
    managed_profile_id: int | None = None,
) -> KnowledgeBase:
    return KnowledgeBase(
        uid=uid,
        name=name,
        embedding_channel_id=channel_id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name=collection_name,
        knowledge_base_type=knowledge_base_type,
        managed_profile_id=managed_profile_id,
        active_embedding_channel_id=channel_id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_signature=f"signature-{collection_name}",
        active_embedding_revision=1,
        active_collection_name=collection_name,
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )


@pytest_asyncio.fixture
async def knowledge_data(db_session: AsyncSession) -> dict[str, Any]:
    owner = User(uid="owner-a", username="owner-a", is_superuser=False)
    other_user = User(uid="owner-b", username="owner-b", is_superuser=False)
    admin = User(uid="admin", username="admin", is_superuser=True)
    channel = ModelChannel(
        name="knowledge-entry-embedding",
        api_key="enc:v1:knowledge-entry-key",
        base_url="https://embedding.invalid/v1",
        model_ids=[
            {
                "model_id": "embed-v1",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "embedding_dimensions": 768,
                "is_enabled": True,
            }
        ],
    )
    db_session.add_all([owner, other_user, admin, channel])
    await db_session.flush()

    owner_prompt = PromptLibrary(uid=owner.uid, name="entry owner prompt", content="owner prompt")
    other_prompt = PromptLibrary(uid=other_user.uid, name="entry other prompt", content="other prompt")
    db_session.add_all([owner_prompt, other_prompt])
    await db_session.flush()
    owner_profile = Profile(uid=owner.uid, name="entry owner profile", prompt_id=owner_prompt.id, configs={})
    other_profile = Profile(uid=other_user.uid, name="entry other profile", prompt_id=other_prompt.id, configs={})
    db_session.add_all([owner_profile, other_profile])
    await db_session.flush()

    user_base = _make_knowledge_base(
        uid=owner.uid,
        name="entry user library",
        collection_name="entry-user-library",
        channel_id=channel.id,
        knowledge_base_type=KnowledgeBaseType.USER,
    )
    managed_base = _make_knowledge_base(
        uid=owner.uid,
        name="entry managed library",
        collection_name="entry-managed-library",
        channel_id=channel.id,
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=owner_profile.id,
    )
    other_user_base = _make_knowledge_base(
        uid=other_user.uid,
        name="other user library",
        collection_name="entry-other-user-library",
        channel_id=channel.id,
        knowledge_base_type=KnowledgeBaseType.USER,
    )
    db_session.add_all([user_base, managed_base, other_user_base])
    await db_session.commit()
    return {
        "owner": owner,
        "other_user": other_user,
        "admin": admin,
        "channel": channel,
        "owner_profile": owner_profile,
        "other_profile": other_profile,
        "user_base": user_base,
        "managed_base": managed_base,
        "other_user_base": other_user_base,
    }


def _authenticate_as(app: FastAPI, user: User) -> None:
    async def override_get_current_user() -> User:
        return user

    app.dependency_overrides[get_current_user] = override_get_current_user


async def _row_counts(db_session: AsyncSession) -> dict[str, int]:
    models = {
        "documents": KnowledgeBaseDocument,
        "items": ManagedKnowledgeItem,
        "revisions": ManagedKnowledgeRevision,
        "jobs": KnowledgeJob,
        "deltas": KnowledgeBaseEmbeddingDelta,
    }
    counts = {}
    for name, model in models.items():
        counts[name] = int(await db_session.scalar(select(func.count()).select_from(model)) or 0)
    return counts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_size", "expected_status"),
    [
        pytest.param(KNOWLEDGE_REQUEST_MAX_BYTES - 1, 200, id="one-byte-under-limit"),
        pytest.param(KNOWLEDGE_REQUEST_MAX_BYTES, 200, id="at-limit"),
        pytest.param(KNOWLEDGE_REQUEST_MAX_BYTES + 1, 413, id="one-byte-over-limit"),
    ],
)
@pytest.mark.parametrize(
    "declared_content_length",
    [None, "1", str(KNOWLEDGE_REQUEST_MAX_BYTES + 100)],
    ids=["missing-content-length", "declared-too-small", "declared-too-large"],
)
async def test_entry_request_limit_uses_received_body_bytes(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    body_size: int,
    expected_status: int,
    declared_content_length: str | None,
) -> None:
    knowledge_base = knowledge_data["user_base"]
    original_content = "complete original"
    request_body = json.dumps(
        {"name": "request-boundary", "content": original_content},
        separators=(",", ":"),
    ).encode("ascii")
    request_body += b" " * (body_size - len(request_body))
    assert len(request_body) == body_size

    async def body_chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(request_body), 64 * 1024):
            yield request_body[offset : offset + 64 * 1024]

    embed_entry = AsyncMock(wraps=knowledge_documents.embed_chunks_with_knowledge_base_config)
    monkeypatch.setattr(knowledge_documents, "embed_chunks_with_knowledge_base_config", embed_entry)
    before = await _row_counts(db_session)
    request = api_client.build_request(
        "POST",
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base.id},
        content=body_chunks(),
        headers={"content-type": "application/json"},
    )
    if declared_content_length is None:
        request.headers.pop("content-length", None)
    else:
        request.headers["content-length"] = declared_content_length

    response = await api_client.send(request)

    assert response.status_code == expected_status
    if expected_status == 200:
        created = response.json()["data"]
        document = await db_session.get(KnowledgeBaseDocument, created["item_id"])
        assert document is not None
        assert document.content == original_content
        collection = vector_backend[knowledge_base.collection_name]
        assert set(collection) == set(document.chunk_ids)
        assert len(collection) == document.chunk_count > 0
        embed_entry.assert_awaited_once()
    else:
        assert response.json()["message"] == t(ERR_KB_REQUEST_TOO_LARGE, max_bytes=KNOWLEDGE_REQUEST_MAX_BYTES)
        assert await _row_counts(db_session) == before
        assert vector_backend == {}
        embed_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_import",
    [False, True],
    ids=["entries-create-json", "documents-import-multipart"],
)
@pytest.mark.parametrize(
    ("content_length", "chunk_overlap", "expected_status"),
    [
        pytest.param(KNOWLEDGE_DOCUMENT_MAX_CHUNKS * 100, 0, 200, id="exact-chunk-limit"),
        pytest.param((KNOWLEDGE_DOCUMENT_MAX_CHUNKS + 1) * 100, 0, 400, id="one-chunk-over-limit"),
        pytest.param(700, 99, 400, id="over-limit-from-overlap"),
    ],
)
async def test_user_document_chunk_count_limit_applies_to_both_ingestion_routes(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    legacy_import: bool,
    content_length: int,
    chunk_overlap: int,
    expected_status: int,
) -> None:
    knowledge_base = knowledge_data["user_base"]
    original_content = "a" * content_length
    embed_entry = AsyncMock(wraps=knowledge_documents.embed_chunks_with_knowledge_base_config)
    monkeypatch.setattr(knowledge_documents, "embed_chunks_with_knowledge_base_config", embed_entry)
    before = await _row_counts(db_session)

    if legacy_import:
        response = await api_client.post(
            "/api/v1/knowledge-base/documents/import",
            params={"kb_id": knowledge_base.id},
            data={"chunk_size": "100", "chunk_overlap": str(chunk_overlap), "batch_size": "16"},
            files={"file": ("chunk-limit.txt", original_content.encode("ascii"), "text/plain")},
        )
    else:
        response = await api_client.post(
            _ENTRY_CREATE_PATH,
            params={"kb_id": knowledge_base.id},
            json={
                "name": "chunk-limit.txt",
                "content": original_content,
                "chunk_size": 100,
                "chunk_overlap": chunk_overlap,
            },
        )

    assert response.status_code == expected_status
    if expected_status == 200:
        document_id = response.json()["data"]["id" if legacy_import else "item_id"]
        document = await db_session.get(KnowledgeBaseDocument, document_id)
        assert document is not None
        assert document.content == original_content
        assert document.chunk_count == KNOWLEDGE_DOCUMENT_MAX_CHUNKS
        collection = vector_backend[knowledge_base.collection_name]
        assert len(collection) == KNOWLEDGE_DOCUMENT_MAX_CHUNKS
        assert set(collection) == set(document.chunk_ids)
    else:
        assert response.json()["message"] == t(ERR_KB_CHUNK_COUNT_EXCEEDED, max_chunks=KNOWLEDGE_DOCUMENT_MAX_CHUNKS)
        assert await _row_counts(db_session) == before
        assert vector_backend == {}
        embed_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_import",
    [False, True],
    ids=["entries-create-json", "documents-import-multipart"],
)
@pytest.mark.parametrize(
    "delta",
    [-1, 0, 1],
    ids=["one-character-under-limit", "at-limit", "one-character-over-limit"],
)
async def test_user_document_chunk_total_chars_limit_applies_to_both_ingestion_routes(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    legacy_import: bool,
    delta: int,
) -> None:
    knowledge_base = knowledge_data["user_base"]
    chunk_size = 1001
    chunk_overlap = 550
    expected_chunk_count = (KNOWLEDGE_DOCUMENT_MAX_TOTAL_CHARS + chunk_size - 1) // chunk_size
    exact_content_length = KNOWLEDGE_DOCUMENT_MAX_TOTAL_CHARS - (expected_chunk_count - 1) * chunk_overlap
    original_content = "a" * (exact_content_length + delta)
    embed_entry = AsyncMock(wraps=knowledge_documents.embed_chunks_with_knowledge_base_config)
    monkeypatch.setattr(knowledge_documents, "embed_chunks_with_knowledge_base_config", embed_entry)
    before = await _row_counts(db_session)

    if legacy_import:
        response = await api_client.post(
            "/api/v1/knowledge-base/documents/import",
            params={"kb_id": knowledge_base.id},
            data={"chunk_size": str(chunk_size), "chunk_overlap": str(chunk_overlap), "batch_size": "16"},
            files={"file": ("chunk-char-limit.txt", original_content.encode("ascii"), "text/plain")},
        )
    else:
        response = await api_client.post(
            _ENTRY_CREATE_PATH,
            params={"kb_id": knowledge_base.id},
            json={
                "name": "chunk-char-limit.txt",
                "content": original_content,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
            },
        )

    expected_status = 400 if delta > 0 else 200
    assert response.status_code == expected_status
    if delta <= 0:
        document_id = response.json()["data"]["id" if legacy_import else "item_id"]
        document = await db_session.get(KnowledgeBaseDocument, document_id)
        assert document is not None
        assert document.content == original_content
        assert document.chunk_count == expected_chunk_count
        collection = vector_backend[knowledge_base.collection_name]
        assert len(collection) == expected_chunk_count
        assert set(collection) == set(document.chunk_ids)
        assert sum(len(item["document"]) for item in collection.values()) == KNOWLEDGE_DOCUMENT_MAX_TOTAL_CHARS + delta
    else:
        assert response.json()["message"] == t(
            ERR_KB_CHUNK_TOTAL_CHARS_EXCEEDED,
            max_chars=KNOWLEDGE_DOCUMENT_MAX_TOTAL_CHARS,
        )
        assert await _row_counts(db_session) == before
        assert vector_backend == {}
        embed_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_key", "legacy_import"),
    [
        pytest.param("user_base", False, id="user-entry-preview"),
        pytest.param("managed_base", False, id="managed-entry-preview"),
        pytest.param("user_base", True, id="user-legacy-import"),
    ],
)
async def test_uploaded_file_over_limit_is_rejected_across_ingestion_routes(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    base_key: str,
    legacy_import: bool,
) -> None:
    knowledge_base = knowledge_data[base_key]
    oversized_file = b"a" * (KNOWLEDGE_FILE_MAX_BYTES + 1)
    embed_entry = AsyncMock(wraps=knowledge_documents.embed_chunks_with_knowledge_base_config)
    monkeypatch.setattr(knowledge_documents, "embed_chunks_with_knowledge_base_config", embed_entry)
    before = await _row_counts(db_session)

    if legacy_import:
        response = await api_client.post(
            "/api/v1/knowledge-base/documents/import",
            params={"kb_id": knowledge_base.id},
            data={"chunk_size": "1000", "chunk_overlap": "100", "batch_size": "16"},
            files={"file": ("oversized.txt", oversized_file, "text/plain")},
        )
    else:
        response = await api_client.post(
            _ENTRY_PREVIEW_PATH,
            params={"kb_id": knowledge_base.id},
            files={"file": ("oversized.txt", oversized_file, "text/plain")},
        )

    assert response.status_code == 413
    assert response.json()["message"] == t(ERR_KB_FILE_TOO_LARGE, max_bytes=KNOWLEDGE_FILE_MAX_BYTES)
    assert await _row_counts(db_session) == before
    assert vector_backend == {}
    embed_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("base_key", ["user_base", "managed_base"], ids=["user", "managed"])
@pytest.mark.parametrize(
    ("content_length", "expected_status"),
    [
        pytest.param(KNOWLEDGE_CONTENT_MAX_CHARS - 1, 200, id="below"),
        pytest.param(KNOWLEDGE_CONTENT_MAX_CHARS, 200, id="exact"),
        pytest.param(KNOWLEDGE_CONTENT_MAX_CHARS + 1, 422, id="above"),
    ],
)
async def test_entry_content_character_limit(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    base_key: str,
    content_length: int,
    expected_status: int,
) -> None:
    knowledge_base = knowledge_data[base_key]
    original_content = " " * (content_length - 1) + "x"
    embed_entry = AsyncMock(wraps=knowledge_documents.embed_chunks_with_knowledge_base_config)
    monkeypatch.setattr(knowledge_documents, "embed_chunks_with_knowledge_base_config", embed_entry)
    before = await _row_counts(db_session)

    response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base.id},
        json={"name": "content-character-limit", "content": original_content},
    )

    assert response.status_code == expected_status
    if expected_status == 422:
        assert await _row_counts(db_session) == before
        assert vector_backend == {}
        embed_entry.assert_not_awaited()
    elif base_key == "user_base":
        created = response.json()["data"]
        assert created["status"] == "ready"
        assert created["item_id"] is not None
        document = await db_session.get(KnowledgeBaseDocument, created["item_id"])
        assert document is not None
        assert document.content == original_content
        collection = vector_backend[knowledge_base.collection_name]
        assert set(collection) == set(document.chunk_ids)
        assert len(collection) == document.chunk_count == 1
        assert [item["document"] for item in collection.values()] == ["x"]
    else:
        created = response.json()["data"]
        assert created["status"] == "created"
        assert created["item_id"] is not None
        assert created["job_id"] is not None
        item = await db_session.get(ManagedKnowledgeItem, created["item_id"])
        job = await db_session.get(KnowledgeJob, created["job_id"])
        assert item is not None and job is not None
        assert item.content == original_content
        assert item.pending_job_id == job.id
        assert job.status == KnowledgeJobStatus.PENDING
        assert vector_backend == {}
        embed_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_key", "legacy_import"),
    [
        pytest.param("user_base", False, id="user-preview"),
        pytest.param("managed_base", False, id="managed-preview"),
        pytest.param("user_base", True, id="user-import"),
    ],
)
@pytest.mark.parametrize("encoding", ["utf-8", "gbk"], ids=["utf8", "gbk"])
@pytest.mark.parametrize(
    ("content_length", "expected_status"),
    [
        pytest.param(KNOWLEDGE_CONTENT_MAX_CHARS, 200, id="exact"),
        pytest.param(KNOWLEDGE_CONTENT_MAX_CHARS + 1, 400, id="over"),
    ],
)
async def test_decoded_file_content_character_limit(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
    base_key: str,
    legacy_import: bool,
    encoding: str,
    content_length: int,
    expected_status: int,
) -> None:
    knowledge_base = knowledge_data[base_key]
    original_content = "中" * content_length
    file_content = original_content.encode(encoding)
    assert len(file_content) < KNOWLEDGE_FILE_MAX_BYTES
    assert len(original_content.encode("utf-8")) < KNOWLEDGE_CONTENT_MAX_BYTES
    embed_entry = AsyncMock(wraps=knowledge_documents.embed_chunks_with_knowledge_base_config)
    monkeypatch.setattr(knowledge_documents, "embed_chunks_with_knowledge_base_config", embed_entry)
    before = await _row_counts(db_session)

    if legacy_import:
        response = await api_client.post(
            "/api/v1/knowledge-base/documents/import",
            params={"kb_id": knowledge_base.id},
            data={"chunk_size": "1000", "chunk_overlap": "100", "batch_size": "16"},
            files={"file": ("character-limit.txt", file_content, "text/plain")},
        )
    else:
        response = await api_client.post(
            _ENTRY_PREVIEW_PATH,
            params={"kb_id": knowledge_base.id},
            files={"file": ("character-limit.txt", file_content, "text/plain")},
        )

    assert response.status_code == expected_status
    if expected_status == 400:
        assert response.json()["message"] == t(ERR_KB_CONTENT_TOO_LONG, max_chars=KNOWLEDGE_CONTENT_MAX_CHARS)
        assert await _row_counts(db_session) == before
        assert vector_backend == {}
        embed_entry.assert_not_awaited()
    elif legacy_import:
        document = await db_session.get(KnowledgeBaseDocument, response.json()["data"]["id"])
        assert document is not None
        assert len(document.content) == content_length
        assert document.content == original_content
        collection = vector_backend[knowledge_base.collection_name]
        assert collection
        assert len(collection) == document.chunk_count
        assert set(collection) == set(document.chunk_ids)
        embed_entry.assert_awaited_once()
    else:
        preview_content = response.json()["data"]["content"]
        assert len(preview_content) == content_length
        assert preview_content == original_content
        assert await _row_counts(db_session) == before
        assert vector_backend == {}
        embed_entry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("base_key", ["user_base", "managed_base"])
@pytest.mark.parametrize(
    ("encoding", "filename"),
    [("utf-8", "utf8.txt"), ("gbk", "gbk.txt"), ("utf-8-sig", "utf8-bom.txt")],
)
async def test_file_preview_decodes_utf8_gbk_and_bom_without_persisting(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
    base_key: str,
    encoding: str,
    filename: str,
) -> None:
    knowledge_base = knowledge_data[base_key]
    original_text = "知识原文\n第二行"
    before = await _row_counts(db_session)

    response = await api_client.post(
        _ENTRY_PREVIEW_PATH,
        params={"kb_id": knowledge_base.id},
        files={"file": (filename, original_text.encode(encoding), "text/plain")},
    )

    assert response.status_code == 200
    preview = response.json()["data"]
    assert preview["filename"] == filename
    assert preview["content"] == original_text
    assert await _row_counts(db_session) == before
    assert vector_backend == {}


@pytest.mark.asyncio
async def test_user_entry_preserves_original_content_chunks_and_legacy_upload(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
) -> None:
    knowledge_base = knowledge_data["user_base"]
    original_text = " \r\n" + ("A" * 260) + "\r\n "
    response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base.id},
        json={
            "name": "manual-entry.txt",
            "content": original_text,
            "source_filename": "picked-source.txt",
            "chunk_size": 100,
            "chunk_overlap": 20,
            "batch_size": 2,
        },
    )

    assert response.status_code == 200
    created = response.json()["data"]
    assert created["status"] == "ready"
    assert created["item_id"] is not None
    assert created["job_id"] is None
    document = await db_session.get(KnowledgeBaseDocument, created["item_id"])
    assert document is not None
    assert document.knowledge_base_id == knowledge_base.id
    assert document.filename == "manual-entry.txt"
    assert document.content == original_text
    assert document.metadata_["source_filename"] == "picked-source.txt"
    assert document.chunk_count > 1
    collection = vector_backend[knowledge_base.collection_name]
    assert set(collection) == set(document.chunk_ids)
    assert len(collection) == document.chunk_count

    content_response = await api_client.get(
        "/api/v1/knowledge-base/documents/get",
        params={"kb_id": knowledge_base.id, "document_id": document.id},
    )
    list_response = await api_client.get(
        "/api/v1/knowledge-base/documents/list",
        params={"kb_id": knowledge_base.id},
    )
    assert content_response.status_code == 200
    assert content_response.json()["data"]["content"] == original_text
    assert list_response.status_code == 200
    listed = list_response.json()["data"]
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == document.id
    assert listed["items"][0]["chunk_count"] == document.chunk_count

    legacy_text = "legacy upload\r\n" + ("B" * 240)
    legacy_response = await api_client.post(
        "/api/v1/knowledge-base/documents/import",
        params={"kb_id": knowledge_base.id},
        data={"chunk_size": "100", "chunk_overlap": "20", "batch_size": "2"},
        files={"file": ("legacy.txt", legacy_text.encode("utf-8"), "text/plain")},
    )
    assert legacy_response.status_code == 200
    legacy_id = legacy_response.json()["data"]["id"]
    legacy_document = await db_session.get(KnowledgeBaseDocument, legacy_id)
    assert legacy_document is not None
    assert legacy_document.filename == "legacy.txt"
    assert legacy_document.content == legacy_text
    assert legacy_document.chunk_count > 1
    assert len(vector_backend[knowledge_base.collection_name]) == document.chunk_count + legacy_document.chunk_count


@pytest.mark.asyncio
async def test_managed_entry_preview_then_create_persists_one_pending_job_and_revision(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
) -> None:
    knowledge_base = knowledge_data["managed_base"]
    original_text = "  托管知识原文\r\n保留首尾空白  "
    preview = await api_client.post(
        _ENTRY_PREVIEW_PATH,
        params={"kb_id": knowledge_base.id},
        files={"file": ("managed-source.md", original_text.encode("utf-8"), "text/markdown")},
    )
    assert preview.status_code == 200
    assert preview.json()["data"]["content"] == original_text
    assert await _row_counts(db_session) == {"documents": 0, "items": 0, "revisions": 0, "jobs": 0, "deltas": 0}
    assert vector_backend == {}

    response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base.id},
        json={"name": "managed source key", "content": original_text, "source_filename": "managed-source.md"},
    )

    assert response.status_code == 200
    created = response.json()["data"]
    assert created["status"] == "created"
    assert created["item_id"] is not None
    assert created["job_id"] is not None
    item = await db_session.get(ManagedKnowledgeItem, created["item_id"])
    job = await db_session.get(KnowledgeJob, created["job_id"])
    assert item is not None and job is not None
    assert item.uid == knowledge_base.uid
    assert item.knowledge_base_id == knowledge_base.id
    assert item.content == original_text
    assert item.source_reference == {"filename": "managed-source.md"}
    assert item.llm_maintainable is False
    assert item.version == 1
    assert item.is_recallable is False
    assert item.pending_job_id == job.id
    assert job.uid == knowledge_base.uid
    assert job.knowledge_base_id == knowledge_base.id
    assert job.operation == KnowledgeJobOperation.MANAGED_CREATE
    assert job.status == KnowledgeJobStatus.PENDING
    assert job.source_profile_id == knowledge_data["owner_profile"].id
    revision = await db_session.scalar(select(ManagedKnowledgeRevision).where(ManagedKnowledgeRevision.knowledge_id == item.id))
    assert revision is not None
    assert revision.version == 1
    assert revision.source_reference == {"filename": "managed-source.md"}
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeBaseDocument)) == 0
    assert vector_backend == {}

    item_response = await api_client.get(
        "/api/v1/knowledge-base/managed-items/get",
        params={"kb_id": knowledge_base.id, "knowledge_id": item.id},
    )
    assert item_response.status_code == 200
    assert item_response.json()["data"]["content"] == original_text
    assert item_response.json()["data"]["llm_maintainable"] is False


@pytest.mark.asyncio
async def test_managed_entry_allows_explicit_llm_maintenance(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
) -> None:
    knowledge_base = knowledge_data["managed_base"]
    response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base.id},
        json={"name": "maintainable entry", "content": "maintainable content", "llm_maintainable": True},
    )

    assert response.status_code == 200
    item = await db_session.get(ManagedKnowledgeItem, response.json()["data"]["item_id"])
    assert item is not None
    assert item.llm_maintainable is True


@pytest.mark.asyncio
async def test_managed_entry_dedupe_replay_conflict_and_duplicate_content_do_not_overwrite(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
) -> None:
    knowledge_base = knowledge_data["managed_base"]
    knowledge_base_id = int(knowledge_base.id)
    first_body = {"name": "dedupe key", "content": "first immutable content", "dedupe_key": "entry-dedupe"}
    first_response = await api_client.post(_ENTRY_CREATE_PATH, params={"kb_id": knowledge_base_id}, json=first_body)
    assert first_response.status_code == 200
    first = first_response.json()["data"]
    initial_counts = await _row_counts(db_session)

    replay_response = await api_client.post(_ENTRY_CREATE_PATH, params={"kb_id": knowledge_base_id}, json=first_body)
    assert replay_response.status_code == 200
    replay = replay_response.json()["data"]
    assert replay["item_id"] == first["item_id"]
    assert replay["job_id"] == first["job_id"]
    assert replay["status"] == "unchanged"

    conflicting_key_response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base_id},
        json={**first_body, "content": "conflicting replacement"},
    )
    assert conflicting_key_response.status_code == 400

    duplicate_content_response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base_id},
        json={"name": "different key same content", "content": first_body["content"], "dedupe_key": "entry-dedupe-2"},
    )
    assert duplicate_content_response.status_code == 200
    duplicate = duplicate_content_response.json()["data"]
    assert duplicate["status"] == "existing_content"
    assert duplicate["item_id"] == first["item_id"]
    assert duplicate["job_id"] is None
    assert await _row_counts(db_session) == initial_counts
    item = await db_session.get(ManagedKnowledgeItem, first["item_id"])
    assert item is not None
    assert item.content == first_body["content"]
    assert item.version == 1


@pytest.mark.asyncio
async def test_managed_entry_over_token_limit_returns_400_without_truncation_or_job(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
) -> None:
    knowledge_base = knowledge_data["managed_base"]
    existing_response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base.id},
        json={"name": "existing content", "content": "keep this complete"},
    )
    assert existing_response.status_code == 200
    existing_id = existing_response.json()["data"]["item_id"]
    before = await _row_counts(db_session)
    oversized_content = "This is a complete token sequence. " * 3_000
    estimated_tokens = estimate_tokens(oversized_content)
    assert len(oversized_content) <= KNOWLEDGE_CONTENT_MAX_CHARS
    assert len(oversized_content.encode("utf-8")) <= KNOWLEDGE_CONTENT_MAX_BYTES
    assert estimated_tokens > MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS

    response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base.id},
        json={"name": "oversized content", "content": oversized_content},
    )

    assert response.status_code == 400
    error_data = response.json()["data"]
    assert error_data["status"] == "content_too_long"
    assert error_data["actual_tokens"] > error_data["max_tokens"] == MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS
    assert await _row_counts(db_session) == before
    assert await db_session.scalar(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_key == "oversized content")) is None
    existing = await db_session.get(ManagedKnowledgeItem, existing_id)
    assert existing is not None
    assert existing.content == "keep this complete"


@pytest.mark.asyncio
async def test_non_owner_is_forbidden_and_superuser_writes_keep_library_owner(
    api_client: AsyncClient,
    knowledge_base_app: FastAPI,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
) -> None:
    _authenticate_as(knowledge_base_app, knowledge_data["other_user"])
    before = await _row_counts(db_session)
    for knowledge_base in (knowledge_data["user_base"], knowledge_data["managed_base"]):
        preview = await api_client.post(
            _ENTRY_PREVIEW_PATH,
            params={"kb_id": knowledge_base.id},
            files={"file": ("private.txt", b"private original", "text/plain")},
        )
        create = await api_client.post(
            _ENTRY_CREATE_PATH,
            params={"kb_id": knowledge_base.id},
            json={"name": "forbidden", "content": "must not be saved"},
        )
        assert preview.status_code == 403
        assert create.status_code == 403
    assert await _row_counts(db_session) == before
    assert vector_backend == {}

    _authenticate_as(knowledge_base_app, knowledge_data["admin"])
    user_response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_data["user_base"].id},
        json={"name": "superuser document", "content": "stored under the base owner"},
    )
    managed_response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_data["managed_base"].id},
        json={"name": "superuser managed item", "content": "managed data belongs to the base owner"},
    )
    assert user_response.status_code == 200
    assert managed_response.status_code == 200
    user_document = await db_session.get(KnowledgeBaseDocument, user_response.json()["data"]["item_id"])
    managed_item = await db_session.get(ManagedKnowledgeItem, managed_response.json()["data"]["item_id"])
    managed_job = await db_session.get(KnowledgeJob, managed_response.json()["data"]["job_id"])
    assert user_document is not None
    assert user_document.knowledge_base_id == knowledge_data["user_base"].id
    assert managed_item is not None and managed_job is not None
    assert managed_item.uid == knowledge_data["owner"].uid
    assert managed_item.knowledge_base_id == knowledge_data["managed_base"].id
    assert managed_job.uid == knowledge_data["owner"].uid


@pytest.mark.asyncio
async def test_entry_validation_errors_do_not_write_any_records(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
) -> None:
    user_base = knowledge_data["user_base"]
    managed_base = knowledge_data["managed_base"]
    user_base_id = int(user_base.id)
    managed_base_id = int(managed_base.id)
    invalid_requests = [
        (user_base_id, {"name": " \t ", "content": "valid content"}, 422),
        (user_base_id, {"name": "x" * 256, "content": "valid content"}, 422),
        (user_base_id, {"name": "unknown field", "content": "valid content", "not_in_contract": True}, 422),
        (user_base_id, {"name": "blank content", "content": " \r\n "}, 400),
        (managed_base_id, {"name": "blank managed content", "content": " \n "}, 400),
        (
            user_base_id,
            {"name": "invalid overlap", "content": "valid content", "chunk_size": 100, "chunk_overlap": 100},
            400,
        ),
    ]
    before = await _row_counts(db_session)

    for kb_id, payload, expected_status in invalid_requests:
        response = await api_client.post(
            _ENTRY_CREATE_PATH,
            params={"kb_id": kb_id},
            json=payload,
        )
        assert response.status_code == expected_status

    assert await _row_counts(db_session) == before
    assert vector_backend == {}


@pytest.mark.asyncio
async def test_vector_write_failure_does_not_save_user_document(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_vector_write(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("vector backend unavailable")

    monkeypatch.setattr(knowledge_documents, "async_upsert_collection_items", fail_vector_write)
    response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_data["user_base"].id},
        json={"name": "vector failure", "content": "this must not persist"},
    )

    assert response.status_code == 500
    assert await _row_counts(db_session) == {"documents": 0, "items": 0, "revisions": 0, "jobs": 0, "deltas": 0}
    assert all(not collection for collection in vector_backend.values())


@pytest.mark.asyncio
async def test_database_save_failure_rolls_back_and_deletes_written_vectors(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_base_id = int(knowledge_data["user_base"].id)
    collection_name = str(knowledge_data["user_base"].collection_name)

    async def fail_document_save(*_args: Any, **_kwargs: Any) -> KnowledgeBaseDocument:
        raise RuntimeError("document persistence failed")

    monkeypatch.setattr(knowledge_documents.knowledge_base_document_crud, "create", fail_document_save)
    response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": user_base_id},
        json={"name": "database failure", "content": "written vectors must be compensated"},
    )

    assert response.status_code == 500
    assert await _row_counts(db_session) == {"documents": 0, "items": 0, "revisions": 0, "jobs": 0, "deltas": 0}
    assert vector_backend[collection_name] == {}


@pytest.mark.asyncio
async def test_user_entry_during_building_migration_records_upsert_delta(
    api_client: AsyncClient,
    db_session: AsyncSession,
    knowledge_data: dict[str, Any],
    vector_backend: dict[str, dict[str, dict[str, Any]]],
) -> None:
    knowledge_base = knowledge_data["user_base"]
    migration_job = KnowledgeJob(
        uid=knowledge_base.uid,
        operation=KnowledgeJobOperation.EMBEDDING_MIGRATION,
        dedupe_key="entry-migration-job",
        request_hash="a" * 64,
        status=KnowledgeJobStatus.PENDING,
        knowledge_base_id=knowledge_base.id,
        payload={"target": "test"},
    )
    db_session.add(migration_job)
    await db_session.flush()
    knowledge_base.migration_job_id = migration_job.id
    knowledge_base.migration_status = KnowledgeBaseMigrationStatus.BUILDING
    await db_session.commit()

    response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base.id},
        json={"name": "migration delta entry", "content": "captured during index build"},
    )

    assert response.status_code == 200
    document_id = response.json()["data"]["item_id"]
    delta = await db_session.scalar(select(KnowledgeBaseEmbeddingDelta).where(KnowledgeBaseEmbeddingDelta.migration_job_id == migration_job.id))
    assert delta is not None
    assert delta.knowledge_base_id == knowledge_base.id
    assert delta.sequence == 1
    assert delta.source_type == KnowledgeBaseMigrationSourceType.USER_DOCUMENT
    assert delta.source_id == document_id
    assert delta.action == KnowledgeBaseMigrationDeltaAction.UPSERT
