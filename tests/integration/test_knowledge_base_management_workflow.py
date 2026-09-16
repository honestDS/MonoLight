from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.api.v1.knowledge_base as knowledge_base_api
import app.core.knowledge.embedding_migration as knowledge_embedding_migration
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.embedding.common import EmbeddingRuntimeConfig, build_embedding_signature
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.knowledge.errors import ManagedKnowledgeConflictError
from app.core.knowledge_jobs.consumer import KnowledgeJobConsumer
from app.core.knowledge_jobs.handlers import create_default_knowledge_job_executor
from app.core.knowledge_jobs.manager import KnowledgeJobTargetBusyError
from app.core.knowledge_jobs.migration import finalize_knowledge_migration_terminal_state
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseDocument,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseMigrationStatus,
    KnowledgeBaseOldCollectionCleanupStatus,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeSourceType,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.providers.database import get_db


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    PromptLibrary.__table__,
                    ModelChannel.__table__,
                    Profile.__table__,
                    KnowledgeBase.__table__,
                    KnowledgeBaseProfileBinding.__table__,
                    KnowledgeBaseDocument.__table__,
                    KnowledgeBaseCollectionOwner.__table__,
                    ManagedKnowledgeItem.__table__,
                    ManagedKnowledgeRevision.__table__,
                    KnowledgeJob.__table__,
                ],
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def knowledge_base_app(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(uid="user-a", is_superuser=False)

    async def fake_load_embedding_model(*_args, **_kwargs):
        return object(), {"model_id": "embed-v1", "embedding_dimensions": 768}

    async def fake_create_collection(*_args, **_kwargs):
        return None

    monkeypatch.setattr(knowledge_base_api, "load_embedding_model", fake_load_embedding_model)
    monkeypatch.setattr(knowledge_base_api, "async_create_collection", fake_create_collection)
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


@pytest_asyncio.fixture
async def embedding_channel(db_session: AsyncSession) -> ModelChannel:
    channel = ModelChannel(
        name="embedding-channel",
        api_key="enc:v1:channel-key",
        base_url="https://embedding.example.com",
        model_ids=[
            {
                "model_id": "embed-v1",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "is_enabled": True,
                "embedding_dimensions": 768,
            }
        ],
    )
    db_session.add(channel)
    await db_session.commit()
    await db_session.refresh(channel)
    return channel


_NULL_FIELDS = (
    "managed_profile_id",
    "target_embedding_channel_id",
    "target_embedding_model_id",
    "target_embedding_dimensions",
    "target_embedding_signature",
    "target_embedding_revision",
    "target_collection_name",
    "migration_job_id",
    "migration_status",
    "migration_snapshot_boundary",
    "migration_cursor",
    "migration_error",
    "migration_started_at",
    "migration_finished_at",
    "old_collection_name",
    "old_collection_cleanup_job_id",
    "old_collection_cleanup_error",
    "old_collection_cleanup_at",
)
_ZERO_FIELDS = (
    "migration_total_count",
    "migration_success_count",
    "migration_failure_count",
    "migration_delta_high_watermark",
    "migration_delta_applied_watermark",
)


def assert_knowledge_base_fields(item: dict | KnowledgeBase, channel_id: int, collection_name: str | None = None) -> str:
    is_response = isinstance(item, dict)
    get = item.get if is_response else lambda field: getattr(item, field)
    collection_name = collection_name or get("collection_name")
    assert collection_name
    expected = {
        "name": "knowledge base workflow",
        "description": "legacy request",
        "embedding_channel_id": channel_id,
        "embedding_model_id": "embed-v1",
        "embedding_dimensions": 768,
        "collection_name": collection_name,
        "knowledge_base_type": "user" if is_response else KnowledgeBaseType.USER,
        "active_embedding_channel_id": channel_id,
        "active_embedding_model_id": "embed-v1",
        "active_embedding_dimensions": 768,
        "active_embedding_signature": build_embedding_signature(channel_id, "embed-v1", 768),
        "active_embedding_revision": 1,
        "active_collection_name": collection_name,
        "old_collection_cleanup_status": "none" if is_response else KnowledgeBaseOldCollectionCleanupStatus.NONE,
        "index_revision": 1,
        "index_status": "ready" if is_response else KnowledgeBaseIndexStatus.READY,
    }
    for field, value in expected.items():
        assert get(field) == value
    for field in _NULL_FIELDS:
        assert get(field) is None
    for field in _ZERO_FIELDS:
        assert get(field) == 0
    if is_response:
        assert get("profile_ids") == []
    return collection_name


@pytest.mark.asyncio
async def test_user_knowledge_base_lifecycle_creates_lists_and_replaces_profile_bindings(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_embedding_model_locks: list[bool] = []

    async def record_load_embedding_model(*_args: object, lock_for_reference_write: bool = False, **_kwargs: object):
        load_embedding_model_locks.append(lock_for_reference_write)
        return object(), {"model_id": "embed-v1", "embedding_dimensions": 768}

    monkeypatch.setattr(knowledge_base_api, "load_embedding_model", record_load_embedding_model)

    response = await api_client.post(
        "/api/v1/knowledge-base/create",
        json={
            "name": "knowledge base workflow",
            "description": "legacy request",
            "embedding_channel_id": embedding_channel.id,
            "embedding_model_id": "embed-v1",
        },
    )

    assert response.status_code == 200
    assert load_embedding_model_locks == [False, True]
    created = response.json()["data"]
    collection_name = assert_knowledge_base_fields(created, embedding_channel.id)

    knowledge_base = await db_session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == "knowledge base workflow"))
    assert knowledge_base is not None
    assert_knowledge_base_fields(knowledge_base, embedding_channel.id, collection_name)

    list_response = await api_client.get("/api/v1/knowledge-base/list")

    assert list_response.status_code == 200
    listed = list_response.json()["data"]["items"][0]
    assert_knowledge_base_fields(listed, embedding_channel.id)

    profile = Profile(uid="user-a", name="binding profile", configs={})
    db_session.add(profile)
    await db_session.flush()
    user_two = KnowledgeBase(
        uid="user-a",
        name="user two",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="binding-user-two",
        knowledge_base_type=KnowledgeBaseType.USER,
    )
    managed = KnowledgeBase(
        uid="user-a",
        name="managed",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="binding-managed",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
    )
    db_session.add_all([user_two, managed])
    await db_session.flush()
    assert knowledge_base.id is not None
    assert profile.id is not None
    assert user_two.id is not None
    assert managed.id is not None
    db_session.add_all(
        [
            KnowledgeBaseProfileBinding(
                uid="user-a",
                knowledge_base_id=knowledge_base.id,
                profile_id=profile.id,
            ),
            KnowledgeBaseProfileBinding(
                uid="user-a",
                knowledge_base_id=managed.id,
                profile_id=profile.id,
            ),
        ]
    )
    await db_session.commit()

    initial_bindings = await api_client.get(f"/api/v1/knowledge-base/profile-bindings?profile_id={profile.id}")
    assert initial_bindings.status_code == 200
    assert initial_bindings.json()["data"] == [knowledge_base.id]

    replaced = await api_client.post(
        f"/api/v1/knowledge-base/profile-bindings?profile_id={profile.id}",
        json={"knowledge_base_ids": [user_two.id]},
    )
    assert replaced.status_code == 200
    assert replaced.json()["data"] == [user_two.id]

    binding_ids = set((await db_session.scalars(select(KnowledgeBaseProfileBinding.knowledge_base_id).where(KnowledgeBaseProfileBinding.profile_id == profile.id))).all())
    assert binding_ids == {user_two.id, managed.id}
    final_bindings = await api_client.get(f"/api/v1/knowledge-base/profile-bindings?profile_id={profile.id}")
    assert final_bindings.status_code == 200
    assert final_bindings.json()["data"] == [user_two.id]


@pytest.mark.asyncio
async def test_delete_user_knowledge_base_queues_all_collection_names(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = PromptLibrary(uid="user-a", name="delete prompt", content="delete prompt content")
    db_session.add(prompt)
    await db_session.flush()

    profile = Profile(uid="user-a", name="delete profile", prompt_id=prompt.id)
    collection_names = (
        "kb-delete-current",
        "kb-delete-active",
        "kb-delete-target",
        "kb-delete-old",
    )
    knowledge_base = KnowledgeBase(
        uid="user-a",
        name="delete knowledge base",
        description="delete test",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name=collection_names[0],
        knowledge_base_type=KnowledgeBaseType.USER,
        active_embedding_channel_id=embedding_channel.id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_revision=1,
        active_collection_name=collection_names[1],
        target_collection_name=collection_names[2],
        old_collection_name=collection_names[3],
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    db_session.add_all([profile, knowledge_base])
    await db_session.flush()

    binding = KnowledgeBaseProfileBinding(
        uid="user-a",
        knowledge_base_id=knowledge_base.id,
        profile_id=profile.id,
    )
    document = KnowledgeBaseDocument(
        knowledge_base_id=knowledge_base.id,
        filename="delete-document.txt",
        content="delete document content",
        chunk_size=1000,
        chunk_overlap=100,
        batch_size=100,
        chunk_count=1,
        chunk_ids=["delete-chunk"],
        metadata_={"source": "test"},
    )
    db_session.add_all([binding, document])
    await db_session.commit()

    response = await api_client.post(f"/api/v1/knowledge-base/delete?kb_id={knowledge_base.id}")

    assert response.status_code == 200
    assert response.json()["data"] is True
    assert await db_session.scalar(select(KnowledgeBase).where(KnowledgeBase.id == knowledge_base.id)) is None
    assert await db_session.scalar(select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.knowledge_base_id == knowledge_base.id)) is None
    assert await db_session.scalar(select(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.knowledge_base_id == knowledge_base.id)) is None

    owners = list((await db_session.scalars(select(KnowledgeBaseCollectionOwner))).all())
    assert len(owners) == 4
    assert {owner.collection_name for owner in owners} == set(collection_names)
    assert {owner.knowledge_base_id for owner in owners} == {None}


@pytest.mark.asyncio
async def test_managed_knowledge_base_rejects_document_import(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
) -> None:
    profile = Profile(uid="user-a", name="managed import profile", configs={})
    db_session.add(profile)
    await db_session.flush()
    managed = KnowledgeBase(
        uid="user-a",
        name="managed import",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="managed-import",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
    )
    db_session.add(managed)
    await db_session.commit()
    await db_session.refresh(managed)

    response = await api_client.post(
        f"/api/v1/knowledge-base/documents/import?kb_id={managed.id}",
        files={"file": ("manual.txt", b"must not be imported", "text/plain")},
    )

    assert response.status_code == 409
    document_count = await db_session.scalar(select(func.count()).select_from(KnowledgeBaseDocument).where(KnowledgeBaseDocument.knowledge_base_id == managed.id))
    assert document_count == 0


async def _execute_managed_knowledge_job(
    db_session: AsyncSession,
    *,
    job_id: int,
) -> KnowledgeJob:
    assert db_session.bind is not None
    session_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    executor = create_default_knowledge_job_executor(session_factory=session_factory)
    consumer = KnowledgeJobConsumer(executor, session_factory=session_factory)

    async def execute_one(target_job_id: int, *, owner: str) -> None:
        async with session_factory() as worker_db:
            claimed = await knowledge_job_crud.try_claim(
                worker_db,
                uid="user-a",
                job_id=target_job_id,
                owner=owner,
                lease_seconds=60,
            )
        assert claimed is not None
        await consumer._execute(claimed, owner)

    owner = f"managed-lifecycle-worker-{job_id}"
    await execute_one(job_id, owner=owner)

    async with session_factory() as worker_db:
        cleanup_jobs = list(
            (
                await worker_db.execute(
                    select(KnowledgeJob).where(
                        KnowledgeJob.parent_job_id == job_id,
                        KnowledgeJob.operation == KnowledgeJobOperation.MANAGED_VECTOR_CLEANUP,
                        KnowledgeJob.status.in_([KnowledgeJobStatus.PENDING, KnowledgeJobStatus.RETRY]),
                    )
                )
            ).scalars()
        )
    for cleanup_job in cleanup_jobs:
        assert cleanup_job.id is not None
        await execute_one(
            cleanup_job.id,
            owner=f"{owner}-cleanup-{cleanup_job.id}",
        )

    db_session.expire_all()
    async with session_factory() as worker_db:
        finished = await knowledge_job_crud.get_by_id(worker_db, uid="user-a", job_id=job_id)
    assert finished is not None
    assert finished.status == KnowledgeJobStatus.SUCCEEDED
    return finished


@pytest.mark.asyncio
async def test_managed_knowledge_user_lifecycle_publishes_reads_updates_preserves_history_and_cleans_delete(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = Profile(uid="user-a", name="managed lifecycle profile", configs={})
    db_session.add(profile)
    await db_session.flush()
    managed = KnowledgeBase(
        uid="user-a",
        name="managed lifecycle",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="managed-lifecycle",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        active_embedding_channel_id=embedding_channel.id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_signature=build_embedding_signature(embedding_channel.id, "embed-v1", 768),
        active_embedding_revision=1,
        active_collection_name="managed-lifecycle",
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    db_session.add(managed)
    await db_session.commit()
    await db_session.refresh(managed)
    assert managed.id is not None
    assert embedding_channel.id is not None
    managed_id = int(managed.id)
    embedding_channel_id = int(embedding_channel.id)

    vectors: dict[str, dict[str, dict[str, object]]] = {}

    async def fake_load_embedding_runtime_config(_db: AsyncSession, channel_id: int, model_id: str):
        assert channel_id == embedding_channel_id
        assert model_id == "embed-v1"
        return EmbeddingRuntimeConfig(
            channel_id=channel_id,
            channel_name="managed-lifecycle",
            model_id=model_id,
            declared_dimensions=768,
            protocol="openai_embedding",
            timeout=30.0,
            base_url="https://embedding.invalid/v1",
            api_key="test-key",
        )

    async def fake_get_or_create_collection(collection_name: str, **_kwargs: object):
        return vectors.setdefault(collection_name, {})

    async def fake_embed(_config: EmbeddingRuntimeConfig, texts: list[str], **_kwargs: object):
        return [[0.01] * 768 for _ in texts]

    async def fake_upsert(
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, object]],
        **_kwargs: object,
    ) -> int:
        collection = vectors.setdefault(collection_name, {})
        for item_id, document, embedding, metadata in zip(
            item_ids,
            documents,
            embeddings,
            metadatas,
            strict=True,
        ):
            collection[item_id] = {
                "document": document,
                "embedding": embedding,
                "metadata": metadata,
            }
        return len(item_ids)

    async def fake_delete(collection_name: str, item_ids: list[str], **_kwargs: object) -> int:
        collection = vectors.setdefault(collection_name, {})
        for item_id in item_ids:
            collection.pop(item_id, None)
        return len(item_ids)

    monkeypatch.setattr("app.core.knowledge_jobs.handlers.load_embedding_runtime_config", fake_load_embedding_runtime_config)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", fake_get_or_create_collection)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", fake_embed)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", fake_upsert)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_delete_collection_items", fake_delete)
    monkeypatch.setattr("app.core.knowledge_jobs.vector_cleanup.async_delete_collection_items", fake_delete)

    create_response = await api_client.post(
        f"/api/v1/knowledge-base/managed-items/create?kb_id={managed_id}",
        json={
            "knowledge_key": "lifecycle-key",
            "content": "alpha managed lifecycle content",
            "llm_maintainable": True,
            "dedupe_key": "managed-lifecycle-create",
        },
    )
    assert create_response.status_code == 200
    create_data = create_response.json()["data"]
    assert create_data["status"] == "created"
    knowledge_id = create_data["item"]["id"]
    create_job_id = create_data["job_id"]
    assert knowledge_id is not None and create_job_id is not None
    create_job = await _execute_managed_knowledge_job(db_session, job_id=create_job_id)
    assert create_job.operation == KnowledgeJobOperation.MANAGED_CREATE
    assert create_job.dedupe_key == "managed-lifecycle-create"

    list_response = await api_client.get(f"/api/v1/knowledge-base/managed-items/list?kb_id={managed_id}&query=alpha")
    assert list_response.status_code == 200
    listed = list_response.json()["data"]
    assert listed["total"] == 1
    assert listed["items"][0]["knowledge_key"] == "lifecycle-key"
    assert listed["items"][0]["content_preview"] == "alpha managed lifecycle content"
    assert listed["items"][0]["publication_job_status"] == "succeeded"
    assert "content" not in listed["items"][0]

    get_response = await api_client.get(f"/api/v1/knowledge-base/managed-items/get?kb_id={managed_id}&knowledge_id={knowledge_id}")
    assert get_response.status_code == 200
    current = get_response.json()["data"]
    assert current["content"] == "alpha managed lifecycle content"
    assert current["version"] == 1
    assert current["indexed_version"] == 1
    assert current["is_recallable"] is True
    assert current["llm_maintainable"] is True

    history_response = await api_client.get(f"/api/v1/knowledge-base/managed-items/history?kb_id={managed_id}&knowledge_id={knowledge_id}")
    assert history_response.status_code == 200
    history = history_response.json()["data"]
    assert [(entry["version"], entry["operation"]) for entry in history] == [(1, "create")]
    assert history[0]["source_type"] == "user_api"
    assert history[0]["modified_by"] == "user"

    jobs_before_stale = await db_session.scalar(select(func.count()).select_from(KnowledgeJob))
    revisions_before_stale = await db_session.scalar(select(func.count()).select_from(ManagedKnowledgeRevision))
    stale_update = await api_client.post(
        f"/api/v1/knowledge-base/managed-items/update?kb_id={managed_id}&knowledge_id={knowledge_id}",
        json={
            "knowledge_key": "lifecycle-key",
            "content": "stale update must not persist",
            "expected_version": 99,
            "llm_maintainable": False,
            "dedupe_key": "managed-lifecycle-stale-update",
        },
    )
    stale_delete = await api_client.post(
        f"/api/v1/knowledge-base/managed-items/delete?kb_id={managed_id}&knowledge_id={knowledge_id}",
        json={"expected_version": 99, "dedupe_key": "managed-lifecycle-stale-delete"},
    )
    assert stale_update.status_code == 409
    assert stale_delete.status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeJob)) == jobs_before_stale
    assert await db_session.scalar(select(func.count()).select_from(ManagedKnowledgeRevision)) == revisions_before_stale

    update_response = await api_client.post(
        f"/api/v1/knowledge-base/managed-items/update?kb_id={managed_id}&knowledge_id={knowledge_id}",
        json={
            "knowledge_key": "lifecycle-key",
            "content": "beta managed lifecycle content",
            "expected_version": 1,
            "llm_maintainable": False,
            "dedupe_key": "managed-lifecycle-update",
        },
    )
    assert update_response.status_code == 200
    update_data = update_response.json()["data"]
    assert update_data["status"] == "updated"
    update_job_id = update_data["job_id"]
    assert update_job_id is not None
    update_job = await _execute_managed_knowledge_job(db_session, job_id=update_job_id)
    assert update_job.operation == KnowledgeJobOperation.MANAGED_UPDATE
    assert update_job.dedupe_key == "managed-lifecycle-update"

    updated_response = await api_client.get(f"/api/v1/knowledge-base/managed-items/get?kb_id={managed_id}&knowledge_id={knowledge_id}")
    assert updated_response.status_code == 200
    updated = updated_response.json()["data"]
    assert updated["content"] == "beta managed lifecycle content"
    assert updated["version"] == 2
    assert updated["indexed_version"] == 2
    assert updated["is_recallable"] is True
    assert updated["llm_maintainable"] is False

    updated_history_response = await api_client.get(f"/api/v1/knowledge-base/managed-items/history?kb_id={managed_id}&knowledge_id={knowledge_id}")
    assert updated_history_response.status_code == 200
    updated_history = updated_history_response.json()["data"]
    assert {(entry["version"], entry["operation"]) for entry in updated_history} == {
        (1, "create"),
        (2, "update"),
    }

    delete_response = await api_client.post(
        f"/api/v1/knowledge-base/managed-items/delete?kb_id={managed_id}&knowledge_id={knowledge_id}",
        json={"expected_version": 2, "dedupe_key": "managed-lifecycle-delete"},
    )
    assert delete_response.status_code == 200
    delete_data = delete_response.json()["data"]
    assert delete_data["status"] == "deleted"
    delete_job_id = delete_data["job_id"]
    assert delete_job_id is not None
    delete_job = await _execute_managed_knowledge_job(db_session, job_id=delete_job_id)
    assert delete_job.operation == KnowledgeJobOperation.MANAGED_DELETE_CLEANUP
    assert delete_job.dedupe_key == "managed-lifecycle-delete"

    missing_response = await api_client.get(f"/api/v1/knowledge-base/managed-items/get?kb_id={managed_id}&knowledge_id={knowledge_id}")
    assert missing_response.status_code == 404
    empty_list = await api_client.get(f"/api/v1/knowledge-base/managed-items/list?kb_id={managed_id}")
    assert empty_list.status_code == 200
    assert empty_list.json()["data"]["total"] == 0

    retained_history_response = await api_client.get(f"/api/v1/knowledge-base/managed-items/history?kb_id={managed_id}&knowledge_id={knowledge_id}")
    assert retained_history_response.status_code == 200
    retained_history = retained_history_response.json()["data"]
    assert {(entry["version"], entry["operation"]) for entry in retained_history} == {
        (1, "create"),
        (2, "update"),
        (3, "delete"),
    }
    assert vectors["managed-lifecycle"] == {}


@pytest.mark.asyncio
async def test_managed_knowledge_failed_publication_retry_api_rebinds_new_job_idempotently(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
) -> None:
    profile = Profile(uid="user-a", name="managed retry profile", configs={})
    db_session.add(profile)
    await db_session.flush()
    managed = KnowledgeBase(
        uid="user-a",
        name="managed retry",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="managed-retry",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
    )
    db_session.add(managed)
    await db_session.flush()
    item = ManagedKnowledgeItem(
        uid="user-a",
        knowledge_base_id=managed.id,
        knowledge_key="retry-key",
        content="retry publication content",
        content_token_count=3,
        content_hash="9" * 64,
        version=1,
        source_type=ManagedKnowledgeSourceType.USER_API,
        created_by=ManagedKnowledgeActorType.USER,
        last_modified_by=ManagedKnowledgeActorType.USER,
        llm_maintainable=False,
        indexed_version=0,
        vector_item_ids=[],
        is_recallable=False,
    )
    db_session.add(item)
    await db_session.flush()
    failed_job = KnowledgeJob(
        uid="user-a",
        operation=KnowledgeJobOperation.MANAGED_CREATE,
        dedupe_key="managed-api-failed-retry-source",
        request_hash="8" * 64,
        status=KnowledgeJobStatus.FAILED,
        knowledge_base_id=managed.id,
        knowledge_id=None,
        expected_version=None,
        payload={},
        error="publication failed",
    )
    db_session.add(failed_job)
    await db_session.flush()
    item.source_job_id = failed_job.id
    await db_session.commit()

    failed_list = await api_client.get(f"/api/v1/knowledge-base/managed-items/list?kb_id={managed.id}")
    assert failed_list.status_code == 200
    failed_row = failed_list.json()["data"]["items"][0]
    assert failed_row["publication_job_id"] == failed_job.id
    assert failed_row["publication_job_status"] == "failed"
    assert failed_row["publication_job_error"] == "publication failed"

    request_body = {
        "expected_version": 1,
        "failed_job_id": failed_job.id,
        "dedupe_key": "managed-api-retry-publication",
    }
    response = await api_client.post(
        f"/api/v1/knowledge-base/managed-items/retry?kb_id={managed.id}&knowledge_id={item.id}",
        json=request_body,
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["status"] == "retry_submitted"
    assert payload["job_id"] is not None
    assert payload["job_id"] != failed_job.id
    await db_session.refresh(item)
    assert item.pending_job_id == payload["job_id"]

    replay = await api_client.post(
        f"/api/v1/knowledge-base/managed-items/retry?kb_id={managed.id}&knowledge_id={item.id}",
        json=request_body,
    )
    assert replay.status_code == 200
    assert replay.json()["data"]["job_id"] == payload["job_id"]

    persisted_failed = await db_session.get(KnowledgeJob, failed_job.id)
    assert persisted_failed is not None
    assert persisted_failed.status == KnowledgeJobStatus.FAILED


@pytest.mark.asyncio
async def test_managed_knowledge_management_api_rolls_back_item_revision_and_job_when_binding_fails(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = Profile(uid="user-a", name="managed rollback profile", configs={})
    db_session.add(profile)
    await db_session.flush()
    managed = KnowledgeBase(
        uid="user-a",
        name="managed rollback",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="managed-rollback",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
    )
    db_session.add(managed)
    await db_session.flush()
    update_item = ManagedKnowledgeItem(
        uid="user-a",
        knowledge_base_id=managed.id,
        knowledge_key="rollback-update-key",
        content="before rollback update",
        content_token_count=3,
        content_hash="5" * 64,
        version=1,
        source_type=ManagedKnowledgeSourceType.USER_API,
        created_by=ManagedKnowledgeActorType.USER,
        last_modified_by=ManagedKnowledgeActorType.USER,
        llm_maintainable=False,
        indexed_version=1,
        is_recallable=True,
    )
    delete_item = ManagedKnowledgeItem(
        uid="user-a",
        knowledge_base_id=managed.id,
        knowledge_key="rollback-delete-key",
        content="before rollback delete",
        content_token_count=3,
        content_hash="6" * 64,
        version=1,
        source_type=ManagedKnowledgeSourceType.USER_API,
        created_by=ManagedKnowledgeActorType.USER,
        last_modified_by=ManagedKnowledgeActorType.USER,
        llm_maintainable=False,
        indexed_version=1,
        is_recallable=True,
    )
    db_session.add_all([update_item, delete_item])
    await db_session.commit()
    await db_session.refresh(update_item)
    await db_session.refresh(delete_item)
    managed_id = managed.id
    update_item_id = update_item.id
    delete_item_id = delete_item.id
    assert managed_id is not None
    assert update_item_id is not None
    assert delete_item_id is not None

    async def fail_binding(*_args, **_kwargs):
        raise ManagedKnowledgeConflictError()

    monkeypatch.setattr(knowledge_base_api.knowledge_job_manager, "_bind_job", fail_binding)

    update_response = await api_client.post(
        f"/api/v1/knowledge-base/managed-items/update?kb_id={managed_id}&knowledge_id={update_item_id}",
        json={
            "knowledge_key": "rollback-update-key",
            "content": "must be rolled back",
            "expected_version": 1,
            "llm_maintainable": True,
            "dedupe_key": "managed-api-rollback-update",
        },
    )
    assert update_response.status_code == 409

    delete_response = await api_client.post(
        f"/api/v1/knowledge-base/managed-items/delete?kb_id={managed_id}&knowledge_id={delete_item_id}",
        json={"expected_version": 1, "dedupe_key": "managed-api-rollback-delete"},
    )
    assert delete_response.status_code == 409

    await db_session.refresh(update_item)
    await db_session.refresh(delete_item)
    assert update_item.version == 1
    assert update_item.content == "before rollback update"
    assert update_item.pending_job_id is None
    assert update_item.llm_maintainable is False
    assert delete_item.version == 1
    assert delete_item.deleted_at is None
    assert delete_item.pending_job_id is None
    assert delete_item.is_recallable is True
    assert await db_session.scalar(select(func.count()).select_from(ManagedKnowledgeRevision).where(ManagedKnowledgeRevision.knowledge_base_id == managed_id)) == 0
    assert await knowledge_job_crud.get_by_dedupe_key(db_session, uid="user-a", dedupe_key="managed-api-rollback-update") is None
    assert await knowledge_job_crud.get_by_dedupe_key(db_session, uid="user-a", dedupe_key="managed-api-rollback-delete") is None


@pytest.mark.asyncio
async def test_user_knowledge_base_rejects_managed_knowledge_management_api(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
) -> None:
    user_base = KnowledgeBase(
        uid="user-a",
        name="user-only documents",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="user-only-documents",
        knowledge_base_type=KnowledgeBaseType.USER,
    )
    db_session.add(user_base)
    await db_session.commit()
    await db_session.refresh(user_base)

    response = await api_client.get(f"/api/v1/knowledge-base/managed-items/list?kb_id={user_base.id}")

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_managed_embedding_follow_workflow_tracks_memory_revision_without_blocking_user_migration(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles = [
        Profile(uid="user-a", name="managed follow one", configs={}),
        Profile(uid="user-a", name="managed follow two", configs={}),
    ]
    target_channel = ModelChannel(
        name="managed-follow-target",
        api_key="enc:v1:managed-follow-target-key",
        base_url="https://managed-follow-target.example.com",
        model_ids=[
            {
                "model_id": "embed-v2",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "is_enabled": True,
                "embedding_dimensions": 1536,
            }
        ],
    )
    db_session.add_all([*profiles, target_channel])
    await db_session.flush()
    managed_bases = [
        KnowledgeBase(
            uid="user-a",
            name=f"managed follow {index}",
            embedding_channel_id=embedding_channel.id,
            embedding_model_id="embed-v1",
            embedding_dimensions=768,
            collection_name=f"managed-follow-{index}",
            knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
            managed_profile_id=profile.id,
            active_embedding_channel_id=embedding_channel.id,
            active_embedding_model_id="embed-v1",
            active_embedding_dimensions=768,
            active_embedding_signature=f"managed-source-{index}",
            active_embedding_revision=1,
            active_collection_name=f"managed-follow-{index}",
            index_revision=1,
            index_status=KnowledgeBaseIndexStatus.READY,
        )
        for index, profile in enumerate(profiles, start=1)
    ]
    user_base = KnowledgeBase(
        uid="user-a",
        name="independent user knowledge base",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="managed-follow-user",
        knowledge_base_type=KnowledgeBaseType.USER,
        active_embedding_channel_id=embedding_channel.id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_signature="user-source-signature",
        active_embedding_revision=1,
        active_collection_name="managed-follow-user",
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    db_session.add_all([*managed_bases, user_base])
    await db_session.commit()
    assert target_channel.id is not None
    assert user_base.id is not None
    managed_ids = [int(base.id) for base in managed_bases if base.id is not None]
    assert len(managed_ids) == 2
    target_channel_id = int(target_channel.id)
    user_base_id = int(user_base.id)

    jobs = await knowledge_embedding_migration.submit_managed_knowledge_base_migrations_for_memory_revision(
        db_session,
        uid="user-a",
        target_channel_id=target_channel_id,
        target_model_id="embed-v2",
        target_dimensions=1536,
        target_signature="memory-target-signature-v2",
        memory_revision=2,
    )
    assert len(jobs) == 2
    first_job_ids = {job.id for job in jobs}
    assert None not in first_job_ids
    assert len(first_job_ids) == 2
    for managed_id in managed_ids:
        current = await db_session.get(KnowledgeBase, managed_id)
        assert current is not None
        assert current.migration_status == KnowledgeBaseMigrationStatus.PREPARING
        assert current.target_embedding_channel_id == target_channel_id
        assert current.target_embedding_model_id == "embed-v2"
        assert current.target_embedding_dimensions == 1536
        assert current.migration_job_id in first_job_ids
    current_user_base = await db_session.get(KnowledgeBase, user_base_id)
    assert current_user_base is not None
    assert current_user_base.migration_job_id is None
    assert current_user_base.target_embedding_model_id is None

    with pytest.raises(KnowledgeJobTargetBusyError):
        await knowledge_embedding_migration.submit_managed_knowledge_base_migrations_for_memory_revision(
            db_session,
            uid="user-a",
            target_channel_id=target_channel_id,
            target_model_id="embed-v3",
            target_dimensions=3072,
            target_signature="memory-target-signature-v3",
            memory_revision=3,
        )
    for managed_id in managed_ids:
        current = await db_session.get(KnowledgeBase, managed_id)
        assert current is not None
        assert current.target_embedding_model_id == "embed-v2"
        assert current.target_embedding_dimensions == 1536
        assert current.migration_job_id in first_job_ids
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(KnowledgeJob)
            .where(
                KnowledgeJob.knowledge_base_id.in_(managed_ids),
                KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION,
            )
        )
        == 2
    )

    async def fake_load_runtime_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(declared_dimensions=1536)

    async def fake_detect_dimensions(_config: object) -> int:
        return 1536

    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr(knowledge_embedding_migration, "detect_embedding_dimensions", fake_detect_dimensions)

    user_response = await api_client.post(
        f"/api/v1/knowledge-base/embedding-migration?kb_id={user_base_id}",
        json={
            "embedding_channel_id": target_channel_id,
            "embedding_model_id": "embed-v2",
        },
    )
    assert user_response.status_code == 200
    user_payload = user_response.json()["data"]
    assert user_payload["migration_status"] == KnowledgeBaseMigrationStatus.PREPARING.value
    assert user_payload["migration_job_id"] not in first_job_ids

    managed_manual_response = await api_client.post(
        f"/api/v1/knowledge-base/embedding-migration?kb_id={managed_ids[0]}",
        json={
            "embedding_channel_id": target_channel_id,
            "embedding_model_id": "embed-v2",
        },
    )
    assert managed_manual_response.status_code == 409
    managed_current = await db_session.get(KnowledgeBase, managed_ids[0])
    assert managed_current is not None
    assert managed_current.migration_job_id in first_job_ids
    assert managed_current.target_embedding_model_id == "embed-v2"


async def _create_user_migration_base(
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    *,
    name: str,
    collection_name: str,
    cleanup_status: KnowledgeBaseOldCollectionCleanupStatus = KnowledgeBaseOldCollectionCleanupStatus.NONE,
) -> KnowledgeBase:
    knowledge_base = KnowledgeBase(
        uid="user-a",
        name=name,
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name=collection_name,
        knowledge_base_type=KnowledgeBaseType.USER,
        active_embedding_channel_id=embedding_channel.id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_signature=build_embedding_signature(embedding_channel.id, "embed-v1", 768),
        active_embedding_revision=1,
        active_collection_name=collection_name,
        old_collection_cleanup_status=cleanup_status,
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    db_session.add(knowledge_base)
    await db_session.commit()
    await db_session.refresh(knowledge_base)
    return knowledge_base


async def _create_migration_target_channel(db_session: AsyncSession, *, name: str) -> ModelChannel:
    target_channel = ModelChannel(
        name=name,
        api_key="enc:v1:migration-target-key",
        base_url="https://migration-target.example.com",
        model_ids=[
            {
                "model_id": "embed-v2",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "is_enabled": True,
                "embedding_dimensions": 1536,
            }
        ],
    )
    db_session.add(target_channel)
    await db_session.commit()
    await db_session.refresh(target_channel)
    return target_channel


@pytest.mark.asyncio
async def test_create_user_knowledge_base_probes_missing_dimensions_and_persists_signature(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_locks: list[bool] = []
    probe_calls = 0

    async def fake_load_embedding_model(*_args: object, lock_for_reference_write: bool = False, **_kwargs: object):
        load_locks.append(lock_for_reference_write)
        return SimpleNamespace(), {"model_id": "embed-v1", "embedding_dimensions": None}

    async def fake_detect_dimensions(_config: object) -> int:
        nonlocal probe_calls
        probe_calls += 1
        return 1024

    monkeypatch.setattr(knowledge_base_api, "load_embedding_model", fake_load_embedding_model)
    monkeypatch.setattr(knowledge_base_api, "detect_embedding_dimensions", fake_detect_dimensions)

    response = await api_client.post(
        "/api/v1/knowledge-base/create",
        json={
            "name": "probed embedding dimensions",
            "description": "persist detected signature",
            "embedding_channel_id": embedding_channel.id,
            "embedding_model_id": "embed-v1",
        },
    )

    assert response.status_code == 200
    created = response.json()["data"]
    assert load_locks == [False, True]
    assert probe_calls == 1
    assert created["active_embedding_dimensions"] == 1024
    assert created["active_embedding_signature"] == build_embedding_signature(embedding_channel.id, "embed-v1", 1024)

    persisted = await db_session.get(KnowledgeBase, created["id"])
    assert persisted is not None
    assert persisted.active_embedding_dimensions == 1024
    assert persisted.active_embedding_signature == build_embedding_signature(embedding_channel.id, "embed-v1", 1024)


@pytest.mark.asyncio
async def test_create_user_knowledge_base_translates_embedding_configuration_change(
    api_client: AsyncClient,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load_embedding_model(*_args: object, lock_for_reference_write: bool = False, **_kwargs: object):
        dimensions = 2048 if lock_for_reference_write else None
        return SimpleNamespace(), {"model_id": "embed-v1", "embedding_dimensions": dimensions}

    async def fake_detect_dimensions(_config: object) -> int:
        return 1024

    monkeypatch.setattr(knowledge_base_api, "load_embedding_model", fake_load_embedding_model)
    monkeypatch.setattr(knowledge_base_api, "detect_embedding_dimensions", fake_detect_dimensions)

    locale_token = set_current_locale("en")
    try:
        response = await api_client.post(
            "/api/v1/knowledge-base/create",
            json={
                "name": "translated embedding configuration change",
                "description": "backend i18n regression",
                "embedding_channel_id": embedding_channel.id,
                "embedding_model_id": "embed-v1",
            },
        )
    finally:
        reset_current_locale(locale_token)

    assert response.status_code == 409
    assert response.json()["message"] == "The knowledge base embedding configuration just changed; please retry"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [KnowledgeJobStatus.FAILED, KnowledgeJobStatus.CANCELLED])
async def test_user_embedding_migration_lifecycle_reuses_active_job_preserves_terminal_target_and_resubmits(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: KnowledgeJobStatus,
) -> None:
    knowledge_base = await _create_user_migration_base(
        db_session,
        embedding_channel,
        name=f"migration lifecycle {terminal_status.value}",
        collection_name=f"migration-lifecycle-{terminal_status.value}",
    )
    target_channel = await _create_migration_target_channel(
        db_session,
        name=f"migration-target-{terminal_status.value}",
    )
    assert knowledge_base.id is not None
    assert target_channel.id is not None
    assert embedding_channel.id is not None
    knowledge_base_id = int(knowledge_base.id)
    knowledge_base_uid = knowledge_base.uid
    source_channel_id = int(embedding_channel.id)
    target_channel_id = int(target_channel.id)

    async def fake_load_runtime_config(
        _db: object,
        channel_id: int,
        model_id: str,
        **_kwargs: object,
    ) -> SimpleNamespace:
        if channel_id == source_channel_id and model_id == "embed-v1":
            return SimpleNamespace(declared_dimensions=768)
        assert channel_id == target_channel_id
        assert model_id == "embed-v2"
        return SimpleNamespace(declared_dimensions=1536)

    async def fake_detect_dimensions(config: SimpleNamespace) -> int:
        assert config.declared_dimensions in {768, 1536}
        return int(config.declared_dimensions)

    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr(knowledge_embedding_migration, "detect_embedding_dimensions", fake_detect_dimensions)

    no_change = await api_client.post(
        f"/api/v1/knowledge-base/embedding-migration?kb_id={knowledge_base_id}",
        json={
            "embedding_channel_id": source_channel_id,
            "embedding_model_id": "embed-v1",
        },
    )
    assert no_change.status_code == 409
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(KnowledgeJob)
            .where(
                KnowledgeJob.knowledge_base_id == knowledge_base_id,
                KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION,
            )
        )
        == 0
    )

    first_response = await api_client.post(
        f"/api/v1/knowledge-base/embedding-migration?kb_id={knowledge_base_id}",
        json={
            "embedding_channel_id": target_channel_id,
            "embedding_model_id": "embed-v2",
        },
    )
    assert first_response.status_code == 200
    first_payload = first_response.json()["data"]
    first_job_id = first_payload["migration_job_id"]
    assert first_job_id is not None
    assert first_payload["active_embedding_model_id"] == "embed-v1"
    assert first_payload["target_embedding_channel_id"] == target_channel_id
    assert first_payload["target_embedding_model_id"] == "embed-v2"
    assert first_payload["target_embedding_dimensions"] == 1536
    assert first_payload["migration_status"] == KnowledgeBaseMigrationStatus.PREPARING.value

    duplicate_response = await api_client.post(
        f"/api/v1/knowledge-base/embedding-migration?kb_id={knowledge_base_id}",
        json={
            "embedding_channel_id": target_channel_id,
            "embedding_model_id": "embed-v2",
        },
    )
    assert duplicate_response.status_code == 200
    assert duplicate_response.json()["data"]["migration_job_id"] == first_job_id
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(KnowledgeJob)
            .where(
                KnowledgeJob.knowledge_base_id == knowledge_base_id,
                KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION,
            )
        )
        == 1
    )

    first_job = await knowledge_job_crud.get_by_id(
        db_session,
        uid=knowledge_base_uid,
        job_id=first_job_id,
    )
    assert first_job is not None
    if terminal_status == KnowledgeJobStatus.CANCELLED:
        cancellation = await knowledge_job_crud.request_cancel(
            db_session,
            uid=knowledge_base_uid,
            job_id=first_job_id,
            commit=False,
        )
        assert cancellation.job is not None
        terminal_job = cancellation.job
    else:
        claimed = await knowledge_job_crud.try_claim(
            db_session,
            uid=knowledge_base_uid,
            job_id=first_job_id,
            owner="migration-terminal-test",
            lease_seconds=60,
        )
        assert claimed is not None
        changed = await knowledge_job_crud.mark_failed(
            db_session,
            uid=knowledge_base_uid,
            job_id=first_job_id,
            owner="migration-terminal-test",
            error="terminal migration failed",
            commit=False,
        )
        assert changed is True
        terminal_job = await knowledge_job_crud.get_by_id(
            db_session,
            uid=knowledge_base_uid,
            job_id=first_job_id,
        )
        assert terminal_job is not None

    target_collection = await finalize_knowledge_migration_terminal_state(
        db_session,
        job=terminal_job,
        error=terminal_job.error,
    )
    assert target_collection is not None
    await db_session.commit()

    current = await db_session.get(KnowledgeBase, knowledge_base_id)
    assert current is not None
    assert current.target_embedding_channel_id is None
    assert current.target_embedding_model_id is None

    list_response = await api_client.get("/api/v1/knowledge-base/list")
    assert list_response.status_code == 200
    listed = next(item for item in list_response.json()["data"]["items"] if item["id"] == knowledge_base_id)
    assert listed["target_embedding_channel_id"] == target_channel_id
    assert listed["target_embedding_model_id"] == "embed-v2"
    assert listed["target_embedding_dimensions"] == 1536

    second_response = await api_client.post(
        f"/api/v1/knowledge-base/embedding-migration?kb_id={knowledge_base_id}",
        json={
            "embedding_channel_id": target_channel_id,
            "embedding_model_id": "embed-v2",
        },
    )
    assert second_response.status_code == 200
    second_payload = second_response.json()["data"]
    second_job_id = second_payload["migration_job_id"]
    assert second_job_id is not None
    assert second_job_id != first_job_id
    assert second_payload["migration_status"] == KnowledgeBaseMigrationStatus.PREPARING.value
    retried = await db_session.get(KnowledgeBase, knowledge_base_id)
    assert retried is not None
    assert retried.migration_job_id == second_job_id
    assert retried.target_embedding_model_id == "embed-v2"
