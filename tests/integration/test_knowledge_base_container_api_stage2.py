from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.api.v1.knowledge_base as knowledge_base_api
import app.core.knowledge.embedding_migration as knowledge_embedding_migration
from app.core.constants import ERR_PROFILE_EMBEDDING_CHANNEL_NOT_FOUND
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.embedding.common import build_embedding_signature
from app.core.exceptions import ParameterException
from app.core.i18n.context import reset_current_locale, set_current_locale
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
    ManagedKnowledgeItem,
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
                    KnowledgeJob.__table__,
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

    async def fake_create_collection(*_args, **_kwargs):
        return None

    monkeypatch.setattr(knowledge_base_api, "load_embedding_model", fake_load_embedding_model)
    monkeypatch.setattr(knowledge_base_api, "async_create_collection", fake_create_collection)
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
async def test_create_and_list_keep_legacy_fields_and_persist_index_state(
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
            "name": "stage2 knowledge base",
            "description": "legacy request",
            "embedding_channel_id": embedding_channel.id,
            "embedding_model_id": "embed-v1",
        },
    )

    assert response.status_code == 200
    assert load_embedding_model_locks == [False, True]
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
async def test_replacing_user_bindings_preserves_managed_binding(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
) -> None:
    profile = Profile(uid="user-a", name="binding profile", configs={})
    db_session.add(profile)
    await db_session.flush()

    user_one = KnowledgeBase(
        uid="user-a",
        name="user one",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="binding-user-one",
        knowledge_base_type=KnowledgeBaseType.USER,
    )
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
    db_session.add_all([user_one, user_two, managed])
    await db_session.flush()
    db_session.add_all(
        [
            KnowledgeBaseProfileBinding(
                uid="user-a",
                knowledge_base_id=user_one.id,
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

    response = await api_client.post(
        f"/api/v1/knowledge-base/profile-bindings?profile_id={profile.id}",
        json={"knowledge_base_ids": [user_two.id]},
    )

    assert response.status_code == 200
    assert response.json()["data"] == [user_two.id]

    binding_ids = set((await db_session.scalars(select(KnowledgeBaseProfileBinding.knowledge_base_id).where(KnowledgeBaseProfileBinding.profile_id == profile.id))).all())
    assert binding_ids == {user_two.id, managed.id}

    get_response = await api_client.get(f"/api/v1/knowledge-base/profile-bindings?profile_id={profile.id}")
    assert get_response.status_code == 200
    assert get_response.json()["data"] == [user_two.id]


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


@pytest.mark.asyncio
async def test_user_knowledge_base_can_submit_embedding_migration_without_memory_enabled(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_channel = ModelChannel(
        name="embedding-target",
        api_key="enc:v1:target-key",
        base_url="https://embedding-target.example.com",
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
    knowledge_base = KnowledgeBase(
        uid="user-a",
        name="manual migration",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="manual-migration-active",
        knowledge_base_type=KnowledgeBaseType.USER,
        active_embedding_channel_id=embedding_channel.id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_signature="manual-source-signature",
        active_embedding_revision=1,
        active_collection_name="manual-migration-active",
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    db_session.add_all([target_channel, knowledge_base])
    await db_session.commit()
    await db_session.refresh(target_channel)
    await db_session.refresh(knowledge_base)

    async def fake_detect_dimensions(_config: object) -> int:
        return 1536

    async def fake_load_runtime_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(declared_dimensions=1536)

    monkeypatch.setattr(knowledge_embedding_migration, "detect_embedding_dimensions", fake_detect_dimensions)
    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", fake_load_runtime_config)

    response = await api_client.post(
        f"/api/v1/knowledge-base/embedding-migration?kb_id={knowledge_base.id}",
        json={
            "embedding_channel_id": target_channel.id,
            "embedding_model_id": "embed-v2",
        },
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["knowledge_base_type"] == "user"
    assert payload["active_embedding_model_id"] == "embed-v1"
    assert payload["target_embedding_channel_id"] == target_channel.id
    assert payload["target_embedding_model_id"] == "embed-v2"
    assert payload["target_embedding_dimensions"] == 1536
    assert payload["migration_status"] == "preparing"
    assert payload["migration_job_id"] is not None

    current = await db_session.get(KnowledgeBase, knowledge_base.id)
    job = await db_session.get(KnowledgeJob, payload["migration_job_id"])
    assert current is not None
    assert current.migration_status == KnowledgeBaseMigrationStatus.PREPARING
    assert current.active_embedding_model_id == "embed-v1"
    assert current.target_embedding_model_id == "embed-v2"
    assert current.target_embedding_dimensions == 1536
    assert job is not None
    assert job.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION
    assert job.status == KnowledgeJobStatus.PENDING


@pytest.mark.asyncio
async def test_managed_knowledge_base_rejects_manual_embedding_migration(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
) -> None:
    profile = Profile(uid="user-a", name="managed migration profile", configs={})
    db_session.add(profile)
    await db_session.flush()
    managed = KnowledgeBase(
        uid="user-a",
        name="managed migration",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="managed-manual-migration",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        active_embedding_channel_id=embedding_channel.id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_signature="managed-source-signature",
        active_embedding_revision=1,
        active_collection_name="managed-manual-migration",
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    db_session.add(managed)
    await db_session.commit()
    await db_session.refresh(managed)

    response = await api_client.post(
        f"/api/v1/knowledge-base/embedding-migration?kb_id={managed.id}",
        json={
            "embedding_channel_id": embedding_channel.id,
            "embedding_model_id": "embed-v1",
        },
    )

    assert response.status_code == 409
    current = await db_session.get(KnowledgeBase, managed.id)
    assert current is not None
    assert current.migration_job_id is None
    assert current.target_embedding_model_id is None


@pytest.mark.asyncio
async def test_managed_knowledge_bases_follow_memory_revision_independently(
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
) -> None:
    profiles = [
        Profile(uid="user-a", name="managed follow one", configs={}),
        Profile(uid="user-a", name="managed follow two", configs={}),
    ]
    db_session.add_all(profiles)
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

    jobs = await knowledge_embedding_migration.submit_managed_knowledge_base_migrations_for_memory_revision(
        db_session,
        uid="user-a",
        target_channel_id=embedding_channel.id,
        target_model_id="embed-v2",
        target_dimensions=1536,
        target_signature="memory-target-signature",
        memory_revision=2,
    )

    assert len(jobs) == 2
    assert len({job.id for job in jobs}) == 2
    for managed in managed_bases:
        current = await db_session.get(KnowledgeBase, managed.id)
        assert current is not None
        assert current.migration_status == KnowledgeBaseMigrationStatus.PREPARING
        assert current.target_embedding_model_id == "embed-v2"
        assert current.target_embedding_dimensions == 1536
        assert current.migration_job_id in {job.id for job in jobs}
    current_user_base = await db_session.get(KnowledgeBase, user_base.id)
    assert current_user_base is not None
    assert current_user_base.migration_job_id is None
    assert current_user_base.target_embedding_model_id is None


@pytest.mark.asyncio
async def test_new_memory_configuration_does_not_overwrite_managed_migration_already_in_progress(
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
) -> None:
    profile = Profile(uid="user-a", name="managed consecutive memory switch", configs={})
    db_session.add(profile)
    await db_session.flush()
    managed = KnowledgeBase(
        uid="user-a",
        name="managed consecutive memory switch",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="managed-consecutive-memory-switch",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        active_embedding_channel_id=embedding_channel.id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_signature="managed-consecutive-source",
        active_embedding_revision=1,
        active_collection_name="managed-consecutive-memory-switch",
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    db_session.add(managed)
    await db_session.commit()
    await db_session.refresh(managed)
    managed_id = managed.id
    assert managed_id is not None

    first_jobs = await knowledge_embedding_migration.submit_managed_knowledge_base_migrations_for_memory_revision(
        db_session,
        uid="user-a",
        target_channel_id=embedding_channel.id,
        target_model_id="embed-v2",
        target_dimensions=1536,
        target_signature="managed-consecutive-target-v2",
        memory_revision=2,
    )
    assert len(first_jobs) == 1
    first_job_id = first_jobs[0].id
    assert first_job_id is not None

    with pytest.raises(KnowledgeJobTargetBusyError):
        await knowledge_embedding_migration.submit_managed_knowledge_base_migrations_for_memory_revision(
            db_session,
            uid="user-a",
            target_channel_id=embedding_channel.id,
            target_model_id="embed-v3",
            target_dimensions=3072,
            target_signature="managed-consecutive-target-v3",
            memory_revision=3,
        )

    current = await db_session.get(KnowledgeBase, managed_id)
    assert current is not None
    assert current.migration_job_id == first_job_id
    assert current.migration_status == KnowledgeBaseMigrationStatus.PREPARING
    assert current.target_embedding_model_id == "embed-v2"
    assert current.target_embedding_dimensions == 1536
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(KnowledgeJob)
            .where(
                KnowledgeJob.knowledge_base_id == managed_id,
                KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION,
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_active_managed_migration_does_not_block_user_knowledge_base_migration(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = Profile(uid="user-a", name="managed active profile", configs={})
    target_channel = ModelChannel(
        name="independent-target",
        api_key="enc:v1:independent-target-key",
        base_url="https://independent-target.example.com",
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
    db_session.add_all([profile, target_channel])
    await db_session.flush()
    managed = KnowledgeBase(
        uid="user-a",
        name="managed active migration",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="managed-active-migration",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        active_embedding_channel_id=embedding_channel.id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_signature="managed-active-source",
        active_embedding_revision=1,
        active_collection_name="managed-active-migration",
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    user_base = KnowledgeBase(
        uid="user-a",
        name="user independent migration",
        embedding_channel_id=embedding_channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=768,
        collection_name="user-independent-migration",
        knowledge_base_type=KnowledgeBaseType.USER,
        active_embedding_channel_id=embedding_channel.id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=768,
        active_embedding_signature="user-independent-source",
        active_embedding_revision=1,
        active_collection_name="user-independent-migration",
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    db_session.add_all([managed, user_base])
    await db_session.commit()

    managed_jobs = await knowledge_embedding_migration.submit_managed_knowledge_base_migrations_for_memory_revision(
        db_session,
        uid="user-a",
        target_channel_id=target_channel.id,
        target_model_id="embed-v2",
        target_dimensions=1536,
        target_signature="managed-independent-target",
        memory_revision=2,
    )
    assert len(managed_jobs) == 1

    async def fake_detect_dimensions(_config: object) -> int:
        return 1536

    async def fake_load_runtime_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(declared_dimensions=1536)

    monkeypatch.setattr(knowledge_embedding_migration, "detect_embedding_dimensions", fake_detect_dimensions)
    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", fake_load_runtime_config)

    response = await api_client.post(
        f"/api/v1/knowledge-base/embedding-migration?kb_id={user_base.id}",
        json={
            "embedding_channel_id": target_channel.id,
            "embedding_model_id": "embed-v2",
        },
    )

    assert response.status_code == 200
    managed_current = await db_session.get(KnowledgeBase, managed.id)
    user_current = await db_session.get(KnowledgeBase, user_base.id)
    assert managed_current is not None
    assert user_current is not None
    assert managed_current.migration_status == KnowledgeBaseMigrationStatus.PREPARING
    assert user_current.migration_status == KnowledgeBaseMigrationStatus.PREPARING
    assert managed_current.migration_job_id != user_current.migration_job_id


async def _create_stage8_user_base(
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


async def _create_stage8_target_channel(db_session: AsyncSession, *, name: str) -> ModelChannel:
    target_channel = ModelChannel(
        name=name,
        api_key="enc:v1:stage8-target-key",
        base_url="https://stage8-target.example.com",
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
async def test_created_user_knowledge_base_rejects_same_embedding_configuration(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_response = await api_client.post(
        "/api/v1/knowledge-base/create",
        json={
            "name": "same embedding configuration",
            "description": "no-op migration guard",
            "embedding_channel_id": embedding_channel.id,
            "embedding_model_id": "embed-v1",
        },
    )
    assert create_response.status_code == 200
    created = create_response.json()["data"]
    assert created["active_embedding_signature"] == build_embedding_signature(embedding_channel.id, "embed-v1", 768)

    async def fake_detect_dimensions(_config: object) -> int:
        return 768

    async def fake_load_runtime_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(declared_dimensions=768)

    monkeypatch.setattr(knowledge_embedding_migration, "detect_embedding_dimensions", fake_detect_dimensions)
    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", fake_load_runtime_config)

    response = await api_client.post(
        f"/api/v1/knowledge-base/embedding-migration?kb_id={created['id']}",
        json={
            "embedding_channel_id": embedding_channel.id,
            "embedding_model_id": "embed-v1",
        },
    )

    assert response.status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


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
async def test_embedding_migration_core_converts_http_exception_to_business_exception(
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_stage8_user_base(
        db_session,
        embedding_channel,
        name="core exception conversion",
        collection_name="core-exception-conversion",
    )

    async def failed_load_runtime_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise HTTPException(status_code=404, detail=ERR_PROFILE_EMBEDDING_CHANNEL_NOT_FOUND)

    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", failed_load_runtime_config)

    with pytest.raises(ParameterException) as exc_info:
        await knowledge_embedding_migration.submit_user_knowledge_base_embedding_migration(
            db_session,
            uid=knowledge_base.uid,
            knowledge_base_id=knowledge_base.id,
            target_channel_id=999999,
            target_model_id="embed-v2",
        )

    assert exc_info.value.code == 404
    assert exc_info.value.message == ERR_PROFILE_EMBEDDING_CHANNEL_NOT_FOUND


@pytest.mark.asyncio
async def test_embedding_migration_probe_failure_leaves_no_target_or_job(
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_stage8_user_base(
        db_session,
        embedding_channel,
        name="probe failure",
        collection_name="probe-failure-active",
    )

    async def fake_load_runtime_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(declared_dimensions=1536)

    async def failed_detect_dimensions(_config: object) -> int:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr(knowledge_embedding_migration, "detect_embedding_dimensions", failed_detect_dimensions)

    with pytest.raises(ParameterException) as exc_info:
        await knowledge_embedding_migration.submit_user_knowledge_base_embedding_migration(
            db_session,
            uid=knowledge_base.uid,
            knowledge_base_id=knowledge_base.id,
            target_channel_id=embedding_channel.id,
            target_model_id="embed-v2",
        )
    assert exc_info.value.code == 502

    current = await db_session.get(KnowledgeBase, knowledge_base.id)
    assert current is not None
    assert current.target_embedding_model_id is None
    assert current.migration_job_id is None
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_embedding_migration_rejects_target_configuration_changed_during_probe(
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_stage8_user_base(
        db_session,
        embedding_channel,
        name="configuration changed",
        collection_name="configuration-changed-active",
    )
    load_count = 0

    async def changing_load_runtime_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal load_count
        load_count += 1
        return SimpleNamespace(declared_dimensions=1536 if load_count == 1 else 3072)

    async def fake_detect_dimensions(_config: object) -> int:
        return 1536

    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", changing_load_runtime_config)
    monkeypatch.setattr(knowledge_embedding_migration, "detect_embedding_dimensions", fake_detect_dimensions)

    with pytest.raises(ParameterException) as exc_info:
        await knowledge_embedding_migration.submit_user_knowledge_base_embedding_migration(
            db_session,
            uid=knowledge_base.uid,
            knowledge_base_id=knowledge_base.id,
            target_channel_id=embedding_channel.id,
            target_model_id="embed-v2",
        )
    assert exc_info.value.code == 409

    current = await db_session.get(KnowledgeBase, knowledge_base.id)
    assert current is not None
    assert current.target_embedding_model_id is None
    assert current.migration_job_id is None
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_embedding_migration_cleanup_failure_blocks_next_submission(
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_stage8_user_base(
        db_session,
        embedding_channel,
        name="cleanup failure blocks migration",
        collection_name="cleanup-failure-blocks-active",
        cleanup_status=KnowledgeBaseOldCollectionCleanupStatus.FAILED,
    )

    async def fake_load_runtime_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(declared_dimensions=1536)

    async def fake_detect_dimensions(_config: object) -> int:
        return 1536

    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr(knowledge_embedding_migration, "detect_embedding_dimensions", fake_detect_dimensions)

    knowledge_base_id = knowledge_base.id
    assert knowledge_base_id is not None
    with pytest.raises(KnowledgeJobTargetBusyError):
        await knowledge_embedding_migration.submit_user_knowledge_base_embedding_migration(
            db_session,
            uid=knowledge_base.uid,
            knowledge_base_id=knowledge_base_id,
            target_channel_id=embedding_channel.id,
            target_model_id="embed-v2",
        )

    current = await db_session.get(KnowledgeBase, knowledge_base_id)
    assert current is not None
    assert current.target_embedding_model_id is None
    assert current.migration_job_id is None
    assert await db_session.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_duplicate_same_target_submission_reuses_active_job(
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_stage8_user_base(
        db_session,
        embedding_channel,
        name="duplicate migration submission",
        collection_name="duplicate-migration-submission",
    )
    target_channel = await _create_stage8_target_channel(db_session, name="duplicate-migration-target")

    async def fake_load_runtime_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(declared_dimensions=1536)

    async def fake_detect_dimensions(_config: object) -> int:
        return 1536

    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr(knowledge_embedding_migration, "detect_embedding_dimensions", fake_detect_dimensions)

    first = await knowledge_embedding_migration.submit_user_knowledge_base_embedding_migration(
        db_session,
        uid=knowledge_base.uid,
        knowledge_base_id=knowledge_base.id,
        target_channel_id=target_channel.id,
        target_model_id="embed-v2",
    )
    second = await knowledge_embedding_migration.submit_user_knowledge_base_embedding_migration(
        db_session,
        uid=knowledge_base.uid,
        knowledge_base_id=knowledge_base.id,
        target_channel_id=target_channel.id,
        target_model_id="embed-v2",
    )

    assert second.id == first.id
    count = await db_session.scalar(select(func.count()).select_from(KnowledgeJob).where(KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION))
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", [KnowledgeJobStatus.FAILED, KnowledgeJobStatus.CANCELLED])
async def test_terminal_migration_keeps_target_visible_and_same_target_can_be_submitted_again(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    monkeypatch: pytest.MonkeyPatch,
    terminal_status: KnowledgeJobStatus,
) -> None:
    knowledge_base = await _create_stage8_user_base(
        db_session,
        embedding_channel,
        name=f"terminal migration {terminal_status.value}",
        collection_name=f"terminal-migration-{terminal_status.value}",
    )
    target_channel = await _create_stage8_target_channel(db_session, name=f"terminal-target-{terminal_status.value}")

    async def fake_load_runtime_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(declared_dimensions=1536)

    async def fake_detect_dimensions(_config: object) -> int:
        return 1536

    monkeypatch.setattr(knowledge_embedding_migration, "load_embedding_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr(knowledge_embedding_migration, "detect_embedding_dimensions", fake_detect_dimensions)

    first = await knowledge_embedding_migration.submit_user_knowledge_base_embedding_migration(
        db_session,
        uid=knowledge_base.uid,
        knowledge_base_id=knowledge_base.id,
        target_channel_id=target_channel.id,
        target_model_id="embed-v2",
    )
    assert first.id is not None

    if terminal_status == KnowledgeJobStatus.CANCELLED:
        cancellation = await knowledge_job_crud.request_cancel(
            db_session,
            uid=knowledge_base.uid,
            job_id=first.id,
            commit=False,
        )
        assert cancellation.job is not None
        terminal_job = cancellation.job
    else:
        claimed = await knowledge_job_crud.try_claim(
            db_session,
            uid=knowledge_base.uid,
            job_id=first.id,
            owner="stage8-terminal-test",
            lease_seconds=60,
        )
        assert claimed is not None
        changed = await knowledge_job_crud.mark_failed(
            db_session,
            uid=knowledge_base.uid,
            job_id=first.id,
            owner="stage8-terminal-test",
            error="terminal migration failed",
            commit=False,
        )
        assert changed is True
        terminal_job = await knowledge_job_crud.get_by_id(db_session, uid=knowledge_base.uid, job_id=first.id)
        assert terminal_job is not None

    target_collection = await finalize_knowledge_migration_terminal_state(
        db_session,
        job=terminal_job,
        error=terminal_job.error,
    )
    assert target_collection is not None
    await db_session.commit()

    current = await db_session.get(KnowledgeBase, knowledge_base.id)
    assert current is not None
    assert current.target_embedding_channel_id is None
    assert current.target_embedding_model_id is None

    list_response = await api_client.get("/api/v1/knowledge-base/list")
    assert list_response.status_code == 200
    listed = next(item for item in list_response.json()["data"]["items"] if item["id"] == knowledge_base.id)
    assert listed["target_embedding_channel_id"] == target_channel.id
    assert listed["target_embedding_model_id"] == "embed-v2"
    assert listed["target_embedding_dimensions"] == 1536

    second = await knowledge_embedding_migration.submit_user_knowledge_base_embedding_migration(
        db_session,
        uid=knowledge_base.uid,
        knowledge_base_id=knowledge_base.id,
        target_channel_id=target_channel.id,
        target_model_id="embed-v2",
    )

    assert second.id is not None
    assert second.id != first.id
    assert second.status == KnowledgeJobStatus.PENDING
    retried = await db_session.get(KnowledgeBase, knowledge_base.id)
    assert retried is not None
    assert retried.migration_job_id == second.id
    assert retried.target_embedding_model_id == "embed-v2"
