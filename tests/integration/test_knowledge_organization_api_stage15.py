from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.api.v1.knowledge_base as knowledge_base_api
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    KnowledgeOrganizationFragment,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationSnapshotItem,
    KnowledgeOrganizationStage,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.providers.database import get_db

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    KnowledgeJob.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeOrganizationSnapshot.__table__,
    KnowledgeOrganizationSnapshotItem.__table__,
    KnowledgeOrganizationStage.__table__,
    KnowledgeOrganizationFragment.__table__,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=_TABLES))

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def test_app(db_session: AsyncSession) -> FastAPI:
    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_get_current_user() -> SimpleNamespace:
        return SimpleNamespace(uid="user-a", is_superuser=False)

    app = FastAPI()
    register_handlers(app)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    app.include_router(knowledge_base_api.router, prefix="/api/v1")
    return app


@pytest_asyncio.fixture
async def api_client(test_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        yield client


def _assert_standard(response: Response, code: int) -> dict:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert isinstance(payload["message"], str)
    assert payload["message"]
    return payload


def _zero_progress() -> dict[str, int]:
    return {
        "stage_count": 0,
        "completed_stage_count": 0,
        "running_stage_count": 0,
        "failed_stage_count": 0,
        "invalidated_stage_count": 0,
        "expected_fragment_count": 0,
        "succeeded_fragment_count": 0,
        "completed_fragment_count": 0,
        "invalidated_fragment_count": 0,
    }


async def _create_managed_knowledge_base(db: AsyncSession, *, suffix: str) -> KnowledgeBase:
    channel = ModelChannel(
        name=f"stage15-organization-channel-{suffix}",
        api_key="stage15-test-key",
        base_url="https://stage15.example.invalid/v1",
        model_ids=[],
    )
    db.add(channel)
    await db.flush()

    prompt = PromptLibrary(
        uid="user-a",
        name=f"stage15-organization-prompt-{suffix}",
        content="stage15 organization prompt",
    )
    db.add(prompt)
    await db.flush()

    profile = Profile(
        uid="user-a",
        name=f"stage15-organization-profile-{suffix}",
        prompt_id=prompt.id,
        configs={},
    )
    db.add(profile)
    await db.flush()

    knowledge_base = KnowledgeBase(
        uid="user-a",
        name=f"stage15 managed knowledge base {suffix}",
        description="stage15 organization API test",
        embedding_channel_id=channel.id,
        embedding_model_id="stage15-embedding-model",
        embedding_dimensions=1536,
        collection_name=f"stage15-organization-{suffix}-collection",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        active_embedding_channel_id=channel.id,
        active_embedding_model_id="stage15-embedding-model",
        active_embedding_dimensions=1536,
        active_embedding_signature=f"stage15-signature-{suffix}",
        active_embedding_revision=1,
        active_collection_name=f"stage15-organization-{suffix}-active",
        index_revision=1,
        index_status=KnowledgeBaseIndexStatus.READY,
    )
    db.add(knowledge_base)
    await db.commit()
    await db.refresh(knowledge_base)
    return knowledge_base


@pytest.mark.asyncio
async def test_submit_knowledge_organization_creates_pending_job_with_zero_progress(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    knowledge_base = await _create_managed_knowledge_base(db_session, suffix="submit")

    response = await api_client.post(
        "/api/v1/knowledge-base/organization",
        params={"kb_id": knowledge_base.id},
    )

    payload = _assert_standard(response, 200)
    data = payload["data"]
    assert data["operation"] == KnowledgeJobOperation.MANUAL_ORGANIZE.value
    assert data["status"] == KnowledgeJobStatus.PENDING.value
    assert data["knowledge_base_id"] == knowledge_base.id
    assert data["snapshot_id"] is None
    assert data["organization_progress"] == _zero_progress()

    job = await db_session.get(KnowledgeJob, data["id"])
    assert job is not None
    assert job.operation == KnowledgeJobOperation.MANUAL_ORGANIZE
    assert job.status == KnowledgeJobStatus.PENDING
    assert job.knowledge_base_id == knowledge_base.id


@pytest.mark.asyncio
async def test_knowledge_organization_job_list_and_detail_expose_snapshot_progress(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    knowledge_base = await _create_managed_knowledge_base(db_session, suffix="views")
    create_response = await api_client.post(
        "/api/v1/knowledge-base/organization",
        params={"kb_id": knowledge_base.id},
    )
    job_id = _assert_standard(create_response, 200)["data"]["id"]

    list_response = await api_client.get(
        "/api/v1/knowledge-base/organization/jobs",
        params={"kb_id": knowledge_base.id},
    )
    list_data = _assert_standard(list_response, 200)["data"]
    assert set(list_data) == {"items", "total", "skip", "limit"}
    assert list_data["total"] == 1
    assert list_data["skip"] == 0
    assert list_data["limit"] == 20
    assert list_data["items"][0]["id"] == job_id
    assert list_data["items"][0]["snapshot_id"] is None
    assert list_data["items"][0]["organization_progress"] == _zero_progress()

    detail_response = await api_client.get(
        f"/api/v1/knowledge-base/organization/{job_id}",
        params={"kb_id": knowledge_base.id},
    )
    detail = _assert_standard(detail_response, 200)["data"]
    assert detail["id"] == job_id
    assert detail["job_id"] == job_id
    assert detail["knowledge_base_id"] == knowledge_base.id
    assert detail["snapshot_id"] is None
    assert detail["organization_progress"] == _zero_progress()


@pytest.mark.asyncio
async def test_cancel_knowledge_organization_job_cancels_pending_job(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    knowledge_base = await _create_managed_knowledge_base(db_session, suffix="cancel")
    create_response = await api_client.post(
        "/api/v1/knowledge-base/organization",
        params={"kb_id": knowledge_base.id},
    )
    job_id = _assert_standard(create_response, 200)["data"]["id"]

    response = await api_client.post(
        f"/api/v1/knowledge-base/organization/{job_id}/cancel",
        params={"kb_id": knowledge_base.id},
    )

    data = _assert_standard(response, 200)["data"]
    assert data["accepted"] is True
    assert data["changed"] is True
    assert data["job"]["id"] == job_id
    assert data["job"]["status"] == KnowledgeJobStatus.CANCELLED.value

    job = await db_session.get(KnowledgeJob, job_id)
    assert job is not None
    assert job.status == KnowledgeJobStatus.CANCELLED
    assert job.active_change_key is None
    assert job.cancel_requested_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [KnowledgeJobStatus.CANCELLED, KnowledgeJobStatus.FAILED],
    ids=["cancelled", "failed"],
)
async def test_retry_knowledge_organization_job_creates_new_job_with_new_snapshot_scope(
    api_client: AsyncClient,
    db_session: AsyncSession,
    terminal_status: KnowledgeJobStatus,
) -> None:
    knowledge_base = await _create_managed_knowledge_base(db_session, suffix=f"retry-{terminal_status.value}")
    create_response = await api_client.post(
        "/api/v1/knowledge-base/organization",
        params={"kb_id": knowledge_base.id},
        json={"dedupe_key": f"stage15-retry-{terminal_status.value}"},
    )
    old_id = _assert_standard(create_response, 200)["data"]["id"]

    if terminal_status == KnowledgeJobStatus.CANCELLED:
        cancel_response = await api_client.post(
            f"/api/v1/knowledge-base/organization/{old_id}/cancel",
            params={"kb_id": knowledge_base.id},
        )
        cancel_data = _assert_standard(cancel_response, 200)["data"]
        assert cancel_data["job"]["status"] == KnowledgeJobStatus.CANCELLED.value
    else:
        old_job = await db_session.get(KnowledgeJob, old_id)
        assert old_job is not None
        old_job.status = KnowledgeJobStatus.FAILED
        old_job.active_change_key = None
        old_job.error = "stage15 test failure"
        old_job.finished_at = old_job.updated_at
        await db_session.commit()

    old_job = await db_session.get(KnowledgeJob, old_id)
    assert old_job is not None
    assert old_job.status == terminal_status
    old_snapshot_nonce = old_job.payload["snapshot_nonce"]

    response = await api_client.post(
        f"/api/v1/knowledge-base/organization/{old_id}/retry",
        params={"kb_id": knowledge_base.id},
    )

    data = _assert_standard(response, 200)["data"]
    assert data["accepted"] is True
    assert data["retry_scope"] == "new_snapshot"
    new_job = data["job"]
    assert new_job["id"] != old_id
    assert new_job["operation"] == KnowledgeJobOperation.MANUAL_ORGANIZE.value
    assert new_job["status"] == KnowledgeJobStatus.PENDING.value
    assert new_job["knowledge_base_id"] == knowledge_base.id
    assert new_job["snapshot_id"] is None
    assert new_job["organization_progress"] == _zero_progress()
    assert new_job["payload"]["snapshot_nonce"] != old_snapshot_nonce

    result = await db_session.execute(select(KnowledgeJob).where(KnowledgeJob.knowledge_base_id == knowledge_base.id).order_by(KnowledgeJob.id))
    jobs = list(result.scalars().all())
    assert len(jobs) == 2
    assert jobs[0].id == old_id
    assert jobs[0].status == terminal_status
    assert jobs[1].id == new_job["id"]
    assert jobs[1].status == KnowledgeJobStatus.PENDING


@pytest.mark.asyncio
async def test_cancel_knowledge_organization_job_rejects_cross_knowledge_base_job(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    target_knowledge_base = await _create_managed_knowledge_base(db_session, suffix="boundary-target")
    other_knowledge_base = await _create_managed_knowledge_base(db_session, suffix="boundary-other")
    create_response = await api_client.post(
        "/api/v1/knowledge-base/organization",
        params={"kb_id": target_knowledge_base.id},
    )
    job_id = _assert_standard(create_response, 200)["data"]["id"]

    response = await api_client.post(
        f"/api/v1/knowledge-base/organization/{job_id}/cancel",
        params={"kb_id": other_knowledge_base.id},
    )

    payload = _assert_standard(response, 404)
    assert payload["data"] is None

    job = await db_session.get(KnowledgeJob, job_id)
    assert job is not None
    assert job.knowledge_base_id == target_knowledge_base.id
    assert job.status == KnowledgeJobStatus.PENDING
    assert job.cancel_requested_at is None
