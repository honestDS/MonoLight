from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.api.v1.knowledge_base as knowledge_base_api
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseDocument,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseOldCollectionCleanupStatus,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseType,
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
                ],
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def test_app(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(uid="user-a", is_superuser=False)

    async def fake_load_embedding_model(*_args, **_kwargs):
        return object(), {"model_id": "embed-v1", "embedding_dimensions": 768}

    monkeypatch.setattr(knowledge_base_api, "load_embedding_model", fake_load_embedding_model)
    monkeypatch.setattr(knowledge_base_api, "create_collection", lambda *_args, **_kwargs: None)
    test_app = FastAPI()
    register_handlers(test_app)
    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_get_current_user
    test_app.include_router(knowledge_base_api.router, prefix="/api/v1")
    return test_app


@pytest_asyncio.fixture
async def api_client(test_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
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
    "active_embedding_signature",
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
        "name": "stage2 knowledge base",
        "description": "legacy request",
        "embedding_channel_id": channel_id,
        "embedding_model_id": "embed-v1",
        "embedding_dimensions": 768,
        "collection_name": collection_name,
        "knowledge_base_type": "user" if is_response else KnowledgeBaseType.USER,
        "active_embedding_channel_id": channel_id,
        "active_embedding_model_id": "embed-v1",
        "active_embedding_dimensions": 768,
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
async def test_create_and_list_keep_legacy_fields_and_persist_index_state(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
) -> None:
    response = await api_client.post(
        "/api/v1/knowledge-base/create",
        json={
            "name": "stage2 knowledge base",
            "description": "legacy request",
            "embedding_channel_id": embedding_channel.id,
            "embedding_model_id": "embed-v1",
        },
    )

    assert response.status_code == 200
    created = response.json()["data"]
    collection_name = assert_knowledge_base_fields(created, embedding_channel.id)

    knowledge_base = await db_session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == "stage2 knowledge base"))
    assert knowledge_base is not None
    assert_knowledge_base_fields(knowledge_base, embedding_channel.id, collection_name)

    list_response = await api_client.get("/api/v1/knowledge-base/list")

    assert list_response.status_code == 200
    listed = list_response.json()["data"]["items"][0]
    assert_knowledge_base_fields(listed, embedding_channel.id)


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

    delete_collection_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def record_delete_collection(*args: object, **kwargs: object) -> None:
        delete_collection_calls.append((args, kwargs))

    monkeypatch.setattr(knowledge_base_api, "delete_collection", record_delete_collection)

    response = await api_client.post(f"/api/v1/knowledge-base/delete?kb_id={knowledge_base.id}")

    assert response.status_code == 200
    assert response.json()["data"] is True
    assert delete_collection_calls == []
    assert await db_session.scalar(select(KnowledgeBase).where(KnowledgeBase.id == knowledge_base.id)) is None
    assert await db_session.scalar(select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.knowledge_base_id == knowledge_base.id)) is None
    assert await db_session.scalar(select(KnowledgeBaseProfileBinding).where(KnowledgeBaseProfileBinding.knowledge_base_id == knowledge_base.id)) is None

    owners = list((await db_session.scalars(select(KnowledgeBaseCollectionOwner))).all())
    assert len(owners) == 4
    assert {owner.collection_name for owner in owners} == set(collection_names)
    assert {owner.knowledge_base_id for owner in owners} == {None}
