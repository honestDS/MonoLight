from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

import app.core.knowledge.documents as knowledge_documents
from app.api.v1 import knowledge_base as knowledge_base_api
from app.core.security import get_current_user
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
    oversized_content = "This is a complete token sequence. " * 20_000

    response = await api_client.post(
        _ENTRY_CREATE_PATH,
        params={"kb_id": knowledge_base.id},
        json={"name": "oversized content", "content": oversized_content},
    )

    assert response.status_code == 400
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
