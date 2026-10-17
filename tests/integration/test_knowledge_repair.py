from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.v1.knowledge_base as knowledge_base_api
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.knowledge_jobs.handlers import create_default_knowledge_job_executor
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseDocument,
    KnowledgeBaseEmbeddingDelta,
    KnowledgeBaseIndexStatus,
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
from tests.integration.test_knowledge_migration import _patch_migration_backend, _VectorBackend

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    User.__table__,
    KnowledgeBase.__table__,
    KnowledgeBaseProfileBinding.__table__,
    KnowledgeBaseDocument.__table__,
    KnowledgeBaseEmbeddingDelta.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeJob.__table__,
)


@pytest_asyncio.fixture
async def repair_database(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(tables=_TABLES, foreign_keys=True) as session_factory:
        yield session_factory


@pytest_asyncio.fixture
async def db_session(repair_database: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with repair_database() as db:
        db.add_all(
            [
                User(uid="repair-owner", username="repair-owner"),
                User(uid="repair-other", username="repair-other"),
                User(uid="repair-admin", username="repair-admin", is_superuser=True),
                User(uid="repair-empty", username="repair-empty"),
            ]
        )
        await db.commit()
        yield db


@pytest.fixture
def repair_identity() -> SimpleNamespace:
    return SimpleNamespace(uid="repair-owner", is_superuser=False)


@pytest.fixture
def repair_app(db_session: AsyncSession, repair_identity: SimpleNamespace) -> FastAPI:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_get_current_user() -> SimpleNamespace:
        return repair_identity

    app = FastAPI()
    register_handlers(app)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.include_router(knowledge_base_api.router, prefix="/api/v1")
    return app


@pytest_asyncio.fixture
async def api_client(repair_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=repair_app), base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def embedding_channel(db_session: AsyncSession) -> ModelChannel:
    channel = ModelChannel(
        name="repair-embedding-channel",
        api_key="enc:v1:repair-test-key",
        base_url="https://embedding.invalid/v1",
        model_ids=[],
    )
    db_session.add(channel)
    await db_session.commit()
    await db_session.refresh(channel)
    return channel


async def _create_knowledge_base(
    db: AsyncSession,
    channel: ModelChannel,
    *,
    uid: str,
    name: str,
    knowledge_base_type: KnowledgeBaseType = KnowledgeBaseType.USER,
) -> KnowledgeBase:
    managed_profile_id = None
    if knowledge_base_type == KnowledgeBaseType.LLM_MANAGED:
        prompt = PromptLibrary(uid=uid, name=f"{name} prompt", content="managed knowledge prompt")
        db.add(prompt)
        await db.flush()
        profile = Profile(uid=uid, name=f"{name} profile", prompt_id=prompt.id, configs={})
        db.add(profile)
        await db.flush()
        managed_profile_id = profile.id

    collection_name = f"repair-{uid}-{name}"
    knowledge_base = KnowledgeBase(
        uid=uid,
        name=name,
        embedding_channel_id=channel.id,
        embedding_model_id="embed-v1",
        embedding_dimensions=2,
        collection_name=collection_name,
        knowledge_base_type=knowledge_base_type,
        managed_profile_id=managed_profile_id,
        active_embedding_channel_id=channel.id,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=2,
        active_embedding_signature="repair-signature-v1",
        active_embedding_revision=1,
        active_collection_name=collection_name,
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    db.add(knowledge_base)
    await db.commit()
    await db.refresh(knowledge_base)
    return knowledge_base


async def _create_busy_migration(db: AsyncSession, knowledge_base: KnowledgeBase) -> KnowledgeJob:
    assert knowledge_base.id is not None
    job = KnowledgeJob(
        uid=knowledge_base.uid,
        operation=KnowledgeJobOperation.EMBEDDING_MIGRATION,
        dedupe_key=f"existing-migration-{knowledge_base.id}",
        request_hash="a" * 64,
        active_change_key=f"kb-migration:{knowledge_base.id}",
        status=KnowledgeJobStatus.PENDING,
        knowledge_base_id=knowledge_base.id,
        payload={"from": {}, "target": {}},
    )
    db.add(job)
    await db.flush()
    knowledge_base.migration_job_id = job.id
    knowledge_base.migration_status = KnowledgeBaseMigrationStatus.PREPARING
    knowledge_base.target_embedding_channel_id = knowledge_base.active_embedding_channel_id
    knowledge_base.target_embedding_model_id = knowledge_base.active_embedding_model_id
    knowledge_base.target_embedding_dimensions = knowledge_base.active_embedding_dimensions
    knowledge_base.target_embedding_signature = knowledge_base.active_embedding_signature
    knowledge_base.target_embedding_revision = knowledge_base.active_embedding_revision + 1
    knowledge_base.target_collection_name = f"busy-target-{knowledge_base.id}"
    await db.commit()
    await db.refresh(job)
    return job


@pytest.mark.asyncio
async def test_manual_repair_rebuilds_one_same_model_base_from_original_text(
    api_client: AsyncClient,
    db_session: AsyncSession,
    repair_database: async_sessionmaker[AsyncSession],
    embedding_channel: ModelChannel,
    repair_identity: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, backend)
    selected = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-owner",
        name="selected",
    )
    sibling = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-owner",
        name="sibling",
    )
    document = KnowledgeBaseDocument(
        knowledge_base_id=selected.id,
        filename="guide.txt",
        content="original repair source text",
        chunk_size=1000,
        chunk_overlap=100,
        batch_size=2,
        chunk_count=1,
        chunk_ids=["legacy-vector-id"],
        metadata_={"document_uuid": "repair-document"},
    )
    db_session.add(document)
    await db_session.commit()

    response = await api_client.post(
        f"/api/v1/knowledge-base/repair?kb_id={selected.id}",
        json={"dedupe_key": "repair-selected-once"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 1
    item = data["items"][0]
    assert item["id"] == selected.id
    assert item["migration_job_id"] is not None
    assert item["migration_status"] == KnowledgeBaseMigrationStatus.PREPARING.value
    assert item["active_embedding_model_id"] == "embed-v1"
    assert item["active_embedding_dimensions"] == 2
    assert item["target_embedding_model_id"] == "embed-v1"
    assert item["target_embedding_dimensions"] == 2
    target_collection = item["target_collection_name"]
    assert target_collection and target_collection != selected.active_collection_name

    jobs = list(
        (
            await db_session.scalars(
                select(KnowledgeJob).where(
                    KnowledgeJob.knowledge_base_id == selected.id,
                    KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION,
                )
            )
        ).all()
    )
    assert len(jobs) == 1
    assert jobs[0].id == item["migration_job_id"]
    assert jobs[0].status == KnowledgeJobStatus.PENDING
    assert (await db_session.get(KnowledgeBase, sibling.id)).migration_job_id is None

    worker_id = f"repair-worker-{jobs[0].id}"
    async with repair_database() as worker_db:
        claimed = await knowledge_job_crud.try_claim(
            worker_db,
            uid=selected.uid,
            job_id=jobs[0].id,
            owner=worker_id,
            lease_seconds=60,
        )
    assert claimed is not None
    executor = create_default_knowledge_job_executor(session_factory=repair_database)
    execution = await executor.execute_claimed(claimed, worker_id)
    assert execution.finalized is True

    await db_session.refresh(selected)
    await db_session.refresh(document)
    completed_job = await knowledge_job_crud.get_by_id(
        db_session,
        uid=selected.uid,
        job_id=jobs[0].id,
    )
    assert selected.migration_status == KnowledgeBaseMigrationStatus.SUCCEEDED
    assert selected.active_collection_name == target_collection
    assert selected.active_embedding_model_id == "embed-v1"
    assert selected.active_embedding_dimensions == 2
    assert selected.migration_total_count == 1
    assert selected.migration_success_count == 1
    assert document.content == "original repair source text"
    assert document.chunk_ids != ["legacy-vector-id"]
    assert completed_job is not None and completed_job.status == KnowledgeJobStatus.SUCCEEDED
    target_items = backend.collections[target_collection]["items"]
    assert isinstance(target_items, dict)
    assert len(target_items) == 1
    rebuilt = next(iter(target_items.values()))
    assert rebuilt["document"] == "original repair source text"
    assert len(rebuilt["embedding"]) == 2

    await db_session.refresh(sibling)
    assert sibling.migration_job_id is None
    assert sibling.active_collection_name == sibling.collection_name

    replay = await api_client.post(
        f"/api/v1/knowledge-base/repair?kb_id={selected.id}",
        json={"dedupe_key": "repair-selected-once"},
    )
    assert replay.status_code == 200
    replay_item = replay.json()["data"]["items"][0]
    assert replay_item["migration_job_id"] == jobs[0].id
    assert replay_item["active_collection_name"] == target_collection
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(KnowledgeJob)
            .where(
                KnowledgeJob.knowledge_base_id == selected.id,
                KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION,
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_superuser_repairs_all_owner_base_types_without_cross_owner_changes(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    repair_identity: SimpleNamespace,
) -> None:
    repair_identity.uid = "repair-admin"
    repair_identity.is_superuser = True
    regular = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-owner",
        name="owner-regular",
    )
    managed = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-owner",
        name="owner-managed",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
    )
    other_owner = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-other",
        name="other-owner",
    )

    response = await api_client.post(
        "/api/v1/knowledge-base/repair?uid=repair-owner",
        json={"dedupe_key": "repair-owner-batch"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 2
    assert {item["id"] for item in data["items"]} == {regular.id, managed.id}
    assert {item["knowledge_base_type"] for item in data["items"]} == {
        KnowledgeBaseType.USER.value,
        KnowledgeBaseType.LLM_MANAGED.value,
    }
    assert len({item["migration_job_id"] for item in data["items"]}) == 2
    await db_session.refresh(other_owner)
    assert other_owner.migration_job_id is None
    assert other_owner.target_collection_name is None

    empty = await api_client.post(
        "/api/v1/knowledge-base/repair?uid=repair-empty",
        json={"dedupe_key": "repair-empty-owner"},
    )
    assert empty.status_code == 200
    assert empty.json()["data"] == {"items": [], "total": 0}


@pytest.mark.asyncio
async def test_repair_enforces_owner_and_admin_uid_validation(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    repair_identity: SimpleNamespace,
) -> None:
    own = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-owner",
        name="own",
    )
    foreign = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-other",
        name="foreign",
    )

    cross_uid = await api_client.post(
        "/api/v1/knowledge-base/repair?uid=repair-other",
        json={"dedupe_key": "forbidden-target-user"},
    )
    cross_kb = await api_client.post(
        f"/api/v1/knowledge-base/repair?kb_id={foreign.id}",
        json={"dedupe_key": "forbidden-target-kb"},
    )
    mismatched_scope = await api_client.post(
        f"/api/v1/knowledge-base/repair?kb_id={own.id}&uid=repair-other",
        json={"dedupe_key": "forbidden-mismatched-scope"},
    )
    assert cross_uid.status_code == 403
    assert cross_kb.status_code == 403
    assert mismatched_scope.status_code == 403

    repair_identity.uid = "repair-admin"
    repair_identity.is_superuser = True
    missing_user = await api_client.post(
        "/api/v1/knowledge-base/repair?uid=repair-missing",
        json={"dedupe_key": "missing-user-target"},
    )
    blank_user = await api_client.post(
        "/api/v1/knowledge-base/repair",
        params={"uid": "   "},
        json={"dedupe_key": "blank-user-target"},
    )
    assert missing_user.status_code == 404
    assert blank_user.status_code == 422


@pytest.mark.asyncio
async def test_repair_deduplicates_submissions_and_rejects_empty_keys(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
) -> None:
    knowledge_base = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-owner",
        name="dedupe",
    )
    invalid = await api_client.post(
        f"/api/v1/knowledge-base/repair?kb_id={knowledge_base.id}",
        json={"dedupe_key": ""},
    )
    assert invalid.status_code == 422

    request = {"dedupe_key": "repair-dedupe-key"}
    first = await api_client.post(
        f"/api/v1/knowledge-base/repair?kb_id={knowledge_base.id}",
        json=request,
    )
    replay = await api_client.post(
        f"/api/v1/knowledge-base/repair?kb_id={knowledge_base.id}",
        json=request,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    first_item = first.json()["data"]["items"][0]
    replay_item = replay.json()["data"]["items"][0]
    assert replay_item["migration_job_id"] == first_item["migration_job_id"]
    assert replay_item["target_collection_name"] == first_item["target_collection_name"]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(KnowledgeJob)
            .where(
                KnowledgeJob.knowledge_base_id == knowledge_base.id,
                KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION,
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_repair_busy_single_and_batch_conflicts_leave_no_partial_submission(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    repair_identity: SimpleNamespace,
) -> None:
    single_busy = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-other",
        name="single-busy",
    )
    existing_single_job = await _create_busy_migration(db_session, single_busy)
    existing_single_job_id = existing_single_job.id
    repair_identity.uid = "repair-other"
    single_response = await api_client.post(
        f"/api/v1/knowledge-base/repair?kb_id={single_busy.id}",
        json={"dedupe_key": "repair-while-busy"},
    )
    assert single_response.status_code == 409
    repair_identity.uid = "repair-owner"
    await db_session.refresh(embedding_channel)

    batch_first = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-owner",
        name="batch-first",
    )
    batch_busy = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-owner",
        name="batch-busy",
    )
    existing_batch_job = await _create_busy_migration(db_session, batch_busy)
    existing_batch_job_id = existing_batch_job.id
    batch_busy_id = batch_busy.id
    repair_identity.is_superuser = False
    batch_response = await api_client.post(
        "/api/v1/knowledge-base/repair",
        json={"dedupe_key": "repair-atomic-batch"},
    )
    assert batch_response.status_code == 409

    await db_session.refresh(batch_first)
    await db_session.refresh(batch_busy)
    assert batch_first.migration_job_id is None
    assert batch_first.target_collection_name is None
    assert batch_busy.migration_job_id == existing_batch_job_id
    assert batch_busy.target_collection_name == f"busy-target-{batch_busy_id}"
    assert existing_single_job_id is not None
    job_count = await db_session.scalar(select(func.count()).select_from(KnowledgeJob).where(KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION))
    assert job_count == 2


@pytest.mark.asyncio
async def test_get_knowledge_base_returns_live_migration_progress_with_owner_checks(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    repair_identity: SimpleNamespace,
) -> None:
    knowledge_base = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-owner",
        name="live-status",
    )
    foreign = await _create_knowledge_base(
        db_session,
        embedding_channel,
        uid="repair-other",
        name="hidden-status",
    )
    submitted = await api_client.post(
        f"/api/v1/knowledge-base/repair?kb_id={knowledge_base.id}",
        json={"dedupe_key": "repair-live-status"},
    )
    assert submitted.status_code == 200
    job_id = submitted.json()["data"]["items"][0]["migration_job_id"]

    knowledge_base.migration_cursor = 3
    knowledge_base.migration_total_count = 5
    knowledge_base.migration_success_count = 2
    knowledge_base.migration_failure_count = 1
    await db_session.commit()

    current = await api_client.get(f"/api/v1/knowledge-base/get?kb_id={knowledge_base.id}")
    denied = await api_client.get(f"/api/v1/knowledge-base/get?kb_id={foreign.id}")
    missing = await api_client.get("/api/v1/knowledge-base/get?kb_id=999999")
    assert current.status_code == 200
    data = current.json()["data"]
    assert data["migration_job_id"] == job_id
    assert data["migration_status"] == KnowledgeBaseMigrationStatus.PREPARING.value
    assert data["migration_cursor"] == 3
    assert data["migration_total_count"] == 5
    assert data["migration_success_count"] == 2
    assert data["migration_failure_count"] == 1
    assert data["target_embedding_model_id"] == "embed-v1"
    assert data["target_embedding_dimensions"] == 2
    assert denied.status_code == 403
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_knowledge_base_list_counts_index_failures_independent_of_page_and_owner_scope(
    api_client: AsyncClient,
    db_session: AsyncSession,
    embedding_channel: ModelChannel,
    repair_identity: SimpleNamespace,
) -> None:
    records = (
        ("owner-pending", "repair-owner", KnowledgeBaseIndexStatus.PENDING, KnowledgeBaseType.USER, None),
        ("owner-ready", "repair-owner", KnowledgeBaseIndexStatus.READY, KnowledgeBaseType.USER, None),
        ("owner-reindexing", "repair-owner", KnowledgeBaseIndexStatus.REINDEXING, KnowledgeBaseType.USER, None),
        (
            "owner-migration-failed",
            "repair-owner",
            KnowledgeBaseIndexStatus.READY,
            KnowledgeBaseType.USER,
            KnowledgeBaseMigrationStatus.FAILED,
        ),
        ("owner-failed", "repair-owner", KnowledgeBaseIndexStatus.FAILED, KnowledgeBaseType.USER, None),
        (
            "owner-managed-failed",
            "repair-owner",
            KnowledgeBaseIndexStatus.FAILED,
            KnowledgeBaseType.LLM_MANAGED,
            None,
        ),
        ("other-failed", "repair-other", KnowledgeBaseIndexStatus.FAILED, KnowledgeBaseType.USER, None),
    )
    knowledge_bases: dict[str, KnowledgeBase] = {}
    for name, uid, index_status, knowledge_base_type, migration_status in records:
        knowledge_base = await _create_knowledge_base(
            db_session,
            embedding_channel,
            uid=uid,
            name=name,
            knowledge_base_type=knowledge_base_type,
        )
        knowledge_base.index_status = index_status
        if migration_status is not None:
            knowledge_base.migration_status = migration_status
        knowledge_bases[name] = knowledge_base
    await db_session.commit()

    owner_response = await api_client.get(
        "/api/v1/knowledge-base/list",
        params={"page": 100, "size": 1},
    )
    assert owner_response.status_code == 200
    owner_data = owner_response.json()["data"]
    assert owner_data["items"] == []
    assert owner_data["repair_required_count"] == 2

    repair_identity.uid = "repair-admin"
    repair_identity.is_superuser = True
    admin_response = await api_client.get(
        "/api/v1/knowledge-base/list",
        params={"page": 100, "size": 1},
    )
    assert admin_response.status_code == 200
    admin_data = admin_response.json()["data"]
    assert admin_data["items"] == []
    assert admin_data["repair_required_count"] == 3

    knowledge_bases["owner-failed"].index_status = KnowledgeBaseIndexStatus.READY
    await db_session.commit()
    decreased_response = await api_client.get(
        "/api/v1/knowledge-base/list",
        params={"page": 100, "size": 1},
    )
    assert decreased_response.status_code == 200
    assert decreased_response.json()["data"]["repair_required_count"] == 2
