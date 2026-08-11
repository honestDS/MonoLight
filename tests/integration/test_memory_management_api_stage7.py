from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.api.v1.memories import router
from app.core.crud.channel import channel_crud
from app.core.crud.memory import (
    memory_embedding_revision_crud,
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.memory import memory_service
from app.core.memory.normalization import build_memory_content_hash, build_memory_record_snapshot, normalize_memory_content
from app.core.security import get_current_user
from app.core.utils.tokenizer import estimate_tokens
from app.handler import register_handlers
from app.models.channel import ChannelCreate, ModelChannel
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryEmbeddingRevisionStatus,
    LongTermMemoryIndexStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryOldCollectionCleanupStatus,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryRevision,
    LongTermMemorySource,
    LongTermMemoryStore,
    LongTermMemoryType,
)
from app.providers.database import get_db

API_TABLES = [
    ModelChannel.__table__,
    LongTermMemoryStore.__table__,
    LongTermMemoryEmbeddingRevision.__table__,
    LongTermMemoryEmbeddingDelta.__table__,
    LongTermMemoryRecord.__table__,
    LongTermMemoryRevision.__table__,
    LongTermMemoryMutationJob.__table__,
]

ORGANIZATION_API_KEY = "stage11-organization-api-key"
ORGANIZATION_BASE_URL = "https://stage11-llm.example/v1"
ORGANIZATION_HTTP_PROXY = "http://stage11-proxy.example:8080"
ORGANIZATION_HEADER_VALUE = "stage11-secret-header"


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MONOLIGH_ENCRYPTION_KEY", "00" * 32)


def _assert_standard(response: httpx.Response, code: int) -> dict:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert isinstance(payload["message"], str)
    assert payload["message"]
    return payload


def _assert_page(payload: dict, *, total: int, page: int = 1, size: int = 20) -> None:
    data = payload["data"]
    assert {"items", "total", "page", "size"}.issubset(data)
    assert data["total"] == total
    assert data["page"] == page
    assert data["size"] == size


async def _create_store(db: AsyncSession, uid: str = "user-a", **overrides: object) -> LongTermMemoryStore:
    values = {
        "active_embedding_channel_id": 1,
        "active_embedding_model_id": "embed-v1",
        "active_embedding_dimensions": 3,
        "active_embedding_signature": "sig-a",
        "active_embedding_revision": 1,
        "active_collection_name": f"collection-{uid}",
        "index_status": LongTermMemoryIndexStatus.READY,
        "old_collection_cleanup_status": LongTermMemoryOldCollectionCleanupStatus.NONE,
    }
    values.update(overrides)
    return await memory_store_crud.create(db, uid=uid, **values)


def _chat_model(model_id: str = "stage11-chat-model", **overrides: object) -> dict:
    model = {
        "model_id": model_id,
        "usage": "CHAT",
        "protocol": "OPENAI",
        "context_window_k": 64,
        "max_tokens": 20_000,
        "temperature": 0.25,
        "top_p": 0.8,
        "is_enabled": True,
        "description": "stage 11 organization model",
        "advanced_settings": {"custom_headers": {"x-stage11-secret": ORGANIZATION_HEADER_VALUE}},
    }
    model.update(overrides)
    return model


async def _create_chat_channel(
    db: AsyncSession,
    *,
    name: str | None = None,
    model_id: str = "stage11-chat-model",
    api_key: str = ORGANIZATION_API_KEY,
    base_url: str = ORGANIZATION_BASE_URL,
    http_proxy: str = ORGANIZATION_HTTP_PROXY,
    model_ids: list[dict] | None = None,
) -> ModelChannel:
    return await channel_crud.create_with_plain_api_key(
        db,
        obj_in=ChannelCreate(
            name=name or f"stage11-chat-channel-{uuid4().hex[:8]}",
            api_key=api_key,
            base_url=base_url,
            http_proxy=http_proxy,
            is_active=True,
            model_ids=model_ids or [_chat_model(model_id=model_id)],
        ),
    )


def _organization_job_payload(*, snapshot_count: int = 2, channel_id: int = 1, model_id: str = "stage11-chat-model") -> dict:
    return {
        "trigger": "manual",
        "snapshot": {"count": snapshot_count},
        "organization_model": {
            "channel_id": channel_id,
            "channel_name": "stage11-secret-channel",
            "model_id": model_id,
            "usage": "CHAT",
            "protocol": "openai",
            "base_url": ORGANIZATION_BASE_URL,
            "api_key": ORGANIZATION_API_KEY,
            "http_proxy": ORGANIZATION_HTTP_PROXY,
            "custom_headers": {"x-stage11-secret": ORGANIZATION_HEADER_VALUE},
            "temperature": 0.25,
            "top_p": 0.8,
            "timeout": 600.0,
            "context_window_k": 64,
            "context_window_tokens": 64_000,
            "max_tokens": 20_000,
            "snapshot_count": snapshot_count,
            "required_output_tokens": snapshot_count * 256,
            "policy_version": 1,
        },
    }


def _assert_no_organization_secrets(value: object) -> None:
    forbidden_keys = {"api_key", "base_url", "http_proxy", "custom_headers"}
    forbidden_values = {
        ORGANIZATION_API_KEY,
        ORGANIZATION_BASE_URL,
        ORGANIZATION_HTTP_PROXY,
        ORGANIZATION_HEADER_VALUE,
    }
    if isinstance(value, dict):
        assert not forbidden_keys.intersection(value)
        for item in value.values():
            _assert_no_organization_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_organization_secrets(item)
    elif isinstance(value, str):
        assert value not in forbidden_values


async def _create_record(
    db: AsyncSession,
    *,
    uid: str = "user-a",
    memory_key: str = "memory-a",
    content: str = "content-a",
    version: int = 1,
    **overrides: object,
) -> LongTermMemoryRecord:
    values = {
        "memory_key": memory_key,
        "memory_type": LongTermMemoryType.FACT,
        "content": content,
        "content_token_count": estimate_tokens(normalize_memory_content(content)),
        "content_hash": build_memory_content_hash(content),
        "version": version,
        "indexed_version": version,
        "source": LongTermMemorySource.USER_API,
        "is_active": True,
        "index_status": LongTermMemoryRecordIndexStatus.READY,
    }
    values.update(overrides)
    return await memory_record_crud.create(db, uid=uid, **values)


async def _create_job(
    db: AsyncSession,
    *,
    uid: str = "user-a",
    dedupe_key: str = "job-a",
    operation: LongTermMemoryMutationOperation = LongTermMemoryMutationOperation.CREATE,
    status: LongTermMemoryMutationStatus = LongTermMemoryMutationStatus.PENDING,
    payload: dict | None = None,
    **values: object,
) -> LongTermMemoryMutationJob:
    job, _created = await memory_job_crud.create(
        db,
        uid=uid,
        operation=operation,
        dedupe_key=dedupe_key,
        status=status,
        payload=payload or {},
        commit=True,
        **values,
    )
    return job


def _publication_payload(*, key: str, content: str) -> dict:
    return {
        "content": content,
        "memory_key": key,
        "content_hash": build_memory_content_hash(content),
        "memory_type": "fact",
        "change_evidence": None,
        "source": "user_api",
        "source_id": None,
        "source_session_id": None,
        "source_profile_id": None,
        "source_message_id": None,
    }


def _migration_payload() -> dict:
    return {
        "from": {
            "channel_id": 1,
            "model_id": "embed-v1",
            "dimensions": 3,
            "signature": "sig-a",
            "collection": "collection-user-a",
            "revision": 1,
        },
        "target": {
            "channel_id": 2,
            "model_id": "embed-v2",
            "dimensions": 4,
            "signature": "sig-b",
            "collection": "collection-user-a-v2",
            "revision": 2,
        },
    }


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=API_TABLES))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def api_app(db_session: AsyncSession) -> tuple[FastAPI, SimpleNamespace]:
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")
    current_user = SimpleNamespace(uid="user-a", is_superuser=False)

    async def override_get_db():
        yield db_session

    def override_get_current_user():
        return current_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return app, current_user


@pytest.mark.asyncio
async def test_memory_crud_api_uses_real_uid_paths_and_standard_responses(api_app: tuple[FastAPI, SimpleNamespace], db_session: AsyncSession) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    first = await _create_record(db_session, memory_key="first", content="first content")
    second = await _create_record(db_session, memory_key="second", content="second content")
    first_id, second_id = first.id, second.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_response = await client.post(
            "/api/v1/memories/create",
            json={
                "dedupe_key": "api-create",
                "content": "created content",
                "memory_key": "created",
                "memory_type": "fact",
                "change_evidence": "user request",
            },
        )
        create_payload = _assert_standard(create_response, 200)
        assert create_payload["data"]["status"] == "accepted"
        assert create_payload["data"]["job"]["operation"] == "create"
        assert "uid" not in create_payload["data"]["job"]

        list_payload = _assert_standard(await client.get("/api/v1/memories/list?size=20"), 200)
        _assert_page(list_payload, total=2)
        listed = next(item for item in list_payload["data"]["items"] if item["id"] == first_id)
        assert listed["content_token_count"] == estimate_tokens(normalize_memory_content("first content"))
        assert listed["pinned"] is False
        assert listed["last_recalled_at"] is None
        get_payload = _assert_standard(await client.get(f"/api/v1/memories/get?memory_id={first_id}"), 200)
        assert get_payload["data"]["id"] == first_id
        assert get_payload["data"]["content_token_count"] == estimate_tokens(normalize_memory_content("first content"))
        assert get_payload["data"]["pinned"] is False
        assert get_payload["data"]["last_recalled_at"] is None

        update_response = await client.post(
            "/api/v1/memories/update",
            json={
                "memory_id": first_id,
                "expected_version": 1,
                "dedupe_key": "api-update",
                "content": "updated content",
                "memory_key": "first-updated",
                "memory_type": "fact",
            },
        )
        update_payload = _assert_standard(update_response, 200)
        assert update_payload["data"]["status"] == "accepted"
        updated = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=first_id)
        assert updated is not None and updated.pending_mutation_job_id == update_payload["data"]["job"]["id"]

        delete_response = await client.post(
            "/api/v1/memories/delete",
            json={"memory_id": second_id, "expected_version": 1, "dedupe_key": "api-delete"},
        )
        delete_payload = _assert_standard(delete_response, 200)
        assert delete_payload["data"]["status"] == "accepted"
        deleted = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=second_id)
        assert deleted is not None and deleted.is_active is False


@pytest.mark.asyncio
async def test_memory_api_rejects_other_user_resources(api_app: tuple[FastAPI, SimpleNamespace], db_session: AsyncSession) -> None:
    app, current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")
    record = await _create_record(db_session)
    record_id = record.id
    await memory_revision_crud.create(
        db_session,
        uid="user-a",
        memory_id=record_id,
        version=1,
        memory_key="memory-a",
        memory_type=LongTermMemoryType.FACT,
        content="content-a",
        content_token_count=estimate_tokens(normalize_memory_content("content-a")),
        content_hash=build_memory_content_hash("content-a"),
        source=LongTermMemorySource.USER_API,
    )
    job = await _create_job(db_session, uid="user-a", dedupe_key="private-job")
    job_id = job.id
    current_user.uid = "user-b"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        list_payload = _assert_standard(await client.get("/api/v1/memories/list"), 200)
        _assert_page(list_payload, total=0)
        requests = [
            client.get(f"/api/v1/memories/get?memory_id={record_id}"),
            client.post(
                "/api/v1/memories/update",
                json={
                    "memory_id": record_id,
                    "expected_version": 1,
                    "content": "other update",
                    "memory_key": "other-key",
                    "memory_type": "fact",
                },
            ),
            client.post("/api/v1/memories/delete", json={"memory_id": record_id}),
            client.get(f"/api/v1/memories/{record_id}/history"),
            client.post(
                f"/api/v1/memories/{record_id}/restore",
                json={"revision_version": 1, "expected_version": 1},
            ),
            client.post(f"/api/v1/memories/{record_id}/resume-current", json={"expected_version": 1}),
            client.get(f"/api/v1/memories/jobs/{job_id}"),
        ]
        responses = [await request for request in requests]
    for response in responses:
        _assert_standard(response, 404)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "extra_field"),
    [
        ("create", "uid"),
        ("create", "source"),
        ("create", "source_message_id"),
        ("create", "collection"),
        ("create", "dimensions"),
        ("create", "importance"),
        ("create", "scope"),
        ("create", "pinned"),
        ("update", "pinned"),
    ],
)
async def test_memory_api_forbids_internal_and_embedding_fields(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
    operation: str,
    extra_field: str,
) -> None:
    app, _current_user = api_app
    body = {
        "content": "content",
        "memory_key": "key",
        "memory_type": "fact",
        extra_field: True if extra_field == "pinned" else 1 if extra_field in {"source_message_id", "dimensions"} else "forbidden",
    }
    if operation == "update":
        await _create_store(db_session)
        record = await _create_record(db_session)
        body.update({"memory_id": record.id, "expected_version": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        payload = _assert_standard(await client.post(f"/api/v1/memories/{operation}", json=body), 422)
    assert payload["data"] is None


@pytest.mark.asyncio
async def test_memory_api_validates_fields_versions_and_state_transitions(api_app: tuple[FastAPI, SimpleNamespace], db_session: AsyncSession) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    record = await _create_record(db_session)
    pending_job = await _create_job(db_session, dedupe_key="pending-job")
    record_id, pending_job_id = record.id, pending_job.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        _assert_standard(await client.get("/api/v1/memories/list?page=0"), 422)
        _assert_standard(await client.get("/api/v1/memories/list?sort_by=invalid"), 422)
        _assert_standard(await client.get("/api/v1/memories/jobs/0"), 422)

        version_conflict = await client.post(
            "/api/v1/memories/update",
            json={
                "memory_id": record_id,
                "expected_version": 9,
                "content": "changed",
                "memory_key": "changed",
                "memory_type": "fact",
            },
        )
        _assert_standard(version_conflict, 409)
        resume_conflict = await client.post(f"/api/v1/memories/{record_id}/resume-current", json={"expected_version": 1})
        _assert_standard(resume_conflict, 409)
        retry_conflict = await client.post(f"/api/v1/memories/jobs/{pending_job_id}/retry")
        _assert_standard(retry_conflict, 409)
        cleanup_conflict = await client.post("/api/v1/memories/collections/1/cleanup-retry", json={"dedupe_key": "cleanup-invalid"})
        _assert_standard(cleanup_conflict, 409)


@pytest.mark.asyncio
async def test_memory_jobs_list_detail_retry_and_cancel(api_app: tuple[FastAPI, SimpleNamespace], db_session: AsyncSession) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    failed = await _create_job(
        db_session,
        dedupe_key="failed-create",
        status=LongTermMemoryMutationStatus.FAILED,
        payload=_publication_payload(key="retry-key", content="retry content"),
    )
    failed_id = failed.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_payload = _assert_standard(
            await client.post(
                "/api/v1/memories/create",
                json={
                    "dedupe_key": "cancel-create",
                    "content": "cancel content",
                    "memory_key": "cancel-key",
                    "memory_type": "fact",
                },
            ),
            200,
        )
        pending_id = create_payload["data"]["job"]["id"]
        jobs_payload = _assert_standard(await client.get("/api/v1/memories/jobs?size=20"), 200)
        _assert_page(jobs_payload, total=2)
        detail_payload = _assert_standard(await client.get(f"/api/v1/memories/jobs/{pending_id}"), 200)
        assert detail_payload["data"]["id"] == pending_id

        cancelled = _assert_standard(await client.post(f"/api/v1/memories/jobs/{pending_id}/cancel"), 200)
        assert cancelled["data"]["accepted"] is True
        assert cancelled["data"]["changed"] is True
        cancelled_again = _assert_standard(await client.post(f"/api/v1/memories/jobs/{pending_id}/cancel"), 200)
        assert cancelled_again["data"]["accepted"] is False
        assert cancelled_again["data"]["changed"] is False

        retried = _assert_standard(await client.post(f"/api/v1/memories/jobs/{failed_id}/retry"), 200)
        assert retried["data"]["status"] == "accepted"
        assert retried["data"]["job"]["id"] != failed_id


@pytest.mark.asyncio
async def test_memory_api_retries_real_failed_delete_cleanup_and_rejects_cancel(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session)
    record = await _create_record(db_session, memory_key="api-cleanup", content="api cleanup content")
    assert record.id is not None
    record_id = int(record.id)
    record_version = record.version
    record_snapshot = build_memory_record_snapshot(record)

    deleted = await memory_service.delete(
        db_session,
        uid="user-a",
        dedupe_key="api-cleanup-delete",
        memory_id=record_id,
        expected_version=record_version,
        source=LongTermMemorySource.USER_API,
        max_attempts=1,
    )
    assert deleted.job is not None and deleted.job.id is not None
    failed_job_id = deleted.job.id
    claimed = await memory_job_crud.try_claim(
        db_session,
        uid="user-a",
        job_id=failed_job_id,
        owner="api-cleanup-failed-worker",
        lease_seconds=30,
        commit=False,
    )
    assert claimed is not None
    failure_result = {
        "phase": "delete_cleanup",
        "memory_id": record_id,
        "record_snapshot": record_snapshot,
    }
    assert await memory_job_crud.mark_failed(
        db_session,
        uid="user-a",
        job_id=failed_job_id,
        owner="api-cleanup-failed-worker",
        error="vector delete failed",
        result=failure_result,
        commit=False,
    )
    await db_session.commit()

    failed = await memory_job_crud.get_by_id(db_session, uid="user-a", job_id=failed_job_id)
    tombstone = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=record_id)
    assert failed is not None
    assert failed.status == LongTermMemoryMutationStatus.FAILED
    assert failed.payload["record_snapshot"] == record_snapshot
    assert failed.result == failure_result
    assert tombstone is not None
    assert tombstone.is_active is False
    assert tombstone.deleted_at is not None
    assert await memory_record_crud.list_recallable_by_ids(db_session, uid="user-a", memory_ids=(record_id,)) == []

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        retried = _assert_standard(await client.post(f"/api/v1/memories/jobs/{failed_job_id}/retry"), 200)

        retry_data = retried["data"]
        assert retry_data["status"] == "accepted"
        new_job_id = retry_data["job"]["id"]
        assert new_job_id != failed_job_id
        assert retry_data["job"]["operation"] == LongTermMemoryMutationOperation.DELETE_CLEANUP.value
        assert retry_data["job"]["payload"]["record_snapshot"] == record_snapshot

        current_user.uid = "user-b"
        foreign_retry = await client.post(f"/api/v1/memories/jobs/{new_job_id}/retry")
        _assert_standard(foreign_retry, 404)
        current_user.uid = "user-a"

        cancelled = _assert_standard(await client.post(f"/api/v1/memories/jobs/{new_job_id}/cancel"), 200)
        assert cancelled["data"]["accepted"] is False
        assert cancelled["data"]["changed"] is False

    retry_job_record = await memory_job_crud.get_by_id(db_session, uid="user-a", job_id=new_job_id)
    retry_record = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=record_id)
    assert retry_job_record is not None
    assert retry_job_record.uid == "user-a"
    assert retry_job_record.status == LongTermMemoryMutationStatus.PENDING
    assert retry_job_record.payload["record_snapshot"] == record_snapshot
    assert retry_job_record.result is None
    assert retry_record is not None
    assert retry_record.pending_mutation_job_id == new_job_id
    assert retry_record.is_active is False
    assert retry_record.deleted_at is not None


@pytest.mark.asyncio
async def test_memory_history_restore_and_resume_current(api_app: tuple[FastAPI, SimpleNamespace], db_session: AsyncSession) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    record = await _create_record(db_session, version=2, content="current content")
    record_id = record.id
    for version, content in ((1, "old content"), (2, "current content")):
        await memory_revision_crud.create(
            db_session,
            uid="user-a",
            memory_id=record_id,
            version=version,
            memory_key="memory-a",
            memory_type=LongTermMemoryType.FACT,
            content=content,
            content_token_count=estimate_tokens(normalize_memory_content(content)),
            content_hash=build_memory_content_hash(content),
            source=LongTermMemorySource.USER_API,
        )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        history = _assert_standard(await client.get(f"/api/v1/memories/{record_id}/history?size=1"), 200)
        _assert_page(history, total=2, size=1)
        assert history["data"]["items"][0]["content_token_count"] == estimate_tokens(normalize_memory_content("current content"))
        restored = _assert_standard(
            await client.post(
                f"/api/v1/memories/{record_id}/restore",
                json={"revision_version": 1, "expected_version": 2, "dedupe_key": "restore-a"},
            ),
            200,
        )
        assert restored["data"]["status"] == "accepted"

    resumed_record = await _create_record(
        db_session,
        memory_key="suppressed",
        content="suppressed content",
        suppress_recall=True,
    )
    resumed_record_id = resumed_record.id
    suppressed_job = await _create_job(
        db_session,
        dedupe_key="suppression-job",
        operation=LongTermMemoryMutationOperation.UPDATE,
        status=LongTermMemoryMutationStatus.FAILED,
        memory_id=resumed_record_id,
        expected_version=1,
    )
    suppressed_job_id = suppressed_job.id
    resumed_record.suppressed_by_job_id = suppressed_job_id
    await db_session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resumed = _assert_standard(
            await client.post(f"/api/v1/memories/{resumed_record_id}/resume-current", json={"expected_version": 1}),
            200,
        )
    assert resumed["data"]["status"] == "resumed"


@pytest.mark.asyncio
async def test_deleted_memory_history_is_read_only_without_current_record(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    record = await _create_record(db_session, content="deleted history content")
    record_id = record.id
    await memory_revision_crud.create(
        db_session,
        uid="user-a",
        memory_id=record_id,
        version=1,
        memory_key=record.memory_key,
        memory_type=record.memory_type,
        content=record.content,
        content_token_count=record.content_token_count,
        content_hash=record.content_hash,
        source=record.source,
        commit=False,
    )
    deleted = await memory_record_crud.delete(
        db_session,
        uid="user-a",
        memory_id=record_id,
        commit=False,
    )
    assert deleted is not None
    await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        history = _assert_standard(
            await client.get(f"/api/v1/memories/{record_id}/history"),
            200,
        )
        restore = await client.post(
            f"/api/v1/memories/{record_id}/restore",
            json={"revision_version": 1, "expected_version": 1},
        )

    _assert_page(history, total=1)
    assert history["data"]["items"][0]["content"] == "deleted history content"
    _assert_standard(restore, 404)


@pytest.mark.asyncio
async def test_memory_api_rejects_overlong_manual_mutations_without_enqueue_or_truncation(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    record = await _create_record(db_session, memory_key="short", content="short content")
    record_id = int(record.id)
    oversized_content = "oversized " * 181

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_response = await client.post(
            "/api/v1/memories/create",
            json={
                "content": oversized_content,
                "memory_key": "too-long-create",
                "memory_type": "fact",
            },
        )
        update_response = await client.post(
            "/api/v1/memories/update",
            json={
                "memory_id": record_id,
                "expected_version": 1,
                "content": oversized_content,
                "memory_key": "too-long-update",
                "memory_type": "fact",
            },
        )

    for response in (create_response, update_response):
        payload = _assert_standard(response, 400)
        assert set(payload["data"]) == {"status", "actual_tokens", "max_tokens", "retryable"}
        assert payload["data"]["status"] == "content_too_long"
        assert payload["data"]["actual_tokens"] > 160
        assert payload["data"]["max_tokens"] == 160
        assert payload["data"]["retryable"] is True

    assert len(oversized_content) < 50000
    assert await memory_job_crud.count(db_session, uid="user-a") == 0
    persisted = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=record_id)
    assert persisted is not None
    assert persisted.content == "short content"
    assert persisted.version == 1


@pytest.mark.asyncio
async def test_memory_api_rejects_overlong_restore_without_enqueue_or_truncation(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    record = await _create_record(db_session, memory_key="restore-target", content="short content")
    record_id = int(record.id)
    oversized_content = "historical oversized " * 181
    await memory_revision_crud.create(
        db_session,
        uid="user-a",
        memory_id=record_id,
        version=2,
        memory_key="historical-oversized",
        memory_type=LongTermMemoryType.FACT,
        content=oversized_content,
        content_token_count=estimate_tokens(normalize_memory_content(oversized_content)),
        content_hash=build_memory_content_hash(oversized_content),
        source=LongTermMemorySource.USER_API,
    )
    await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/memories/{record_id}/restore",
            json={"revision_version": 2, "expected_version": 1},
        )

    payload = _assert_standard(response, 400)
    assert set(payload["data"]) == {"status", "actual_tokens", "max_tokens", "retryable"}
    assert payload["data"]["status"] == "content_too_long"
    assert payload["data"]["actual_tokens"] > 160
    assert payload["data"]["max_tokens"] == 160
    assert payload["data"]["retryable"] is True
    assert await memory_job_crud.count(db_session, uid="user-a") == 0
    persisted = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=record_id)
    assert persisted is not None
    assert persisted.content == "short content"
    assert persisted.version == 1
    historical = await memory_revision_crud.get_by_memory_id(db_session, uid="user-a", memory_id=record_id, version=2)
    assert historical is not None
    assert historical.content == oversized_content
    assert historical.content_token_count > 160


@pytest.mark.asyncio
async def test_memory_settings_and_reindex_api_with_state_guard(api_app: tuple[FastAPI, SimpleNamespace], db_session: AsyncSession) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settings = _assert_standard(await client.get("/api/v1/memories/settings"), 200)
        assert settings["data"]["configured"] is True
        assert settings["data"]["active"]["revision"] == 1
        assert settings["data"]["capacity"] == {
            "max_active_records": 50,
            "organize_trigger_records": 45,
            "content_max_tokens": 160,
            "active_record_count": 0,
            "status": "normal",
        }
        assert settings["data"]["store"]["content_max_tokens"] == 160
        assert settings["data"]["store"]["active_record_count"] == 0
        reindex = _assert_standard(await client.post("/api/v1/memories/reindex", json={"dedupe_key": "reindex-a"}), 200)
        assert reindex["data"]["created"] is True
        assert reindex["data"]["job"]["operation"] == "reindex"
        blocked = await client.post("/api/v1/memories/reindex", json={"dedupe_key": "reindex-b"})
        _assert_standard(blocked, 409)


@pytest.mark.asyncio
async def test_memory_settings_returns_fixed_capacity_without_store(api_app: tuple[FastAPI, SimpleNamespace]) -> None:
    app, _current_user = api_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settings = _assert_standard(await client.get("/api/v1/memories/settings"), 200)

    assert settings["data"]["configured"] is False
    assert settings["data"]["capacity"] == {
        "max_active_records": 50,
        "organize_trigger_records": 45,
        "content_max_tokens": 160,
        "active_record_count": 0,
        "status": "normal",
    }
    assert settings["data"]["store"] == {
        "content_max_tokens": 160,
        "active_record_count": 0,
    }


@pytest.mark.asyncio
async def test_memory_embedding_migration_list_detail_retry_and_cancel(api_app: tuple[FastAPI, SimpleNamespace], db_session: AsyncSession) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    payload = _migration_payload()
    old_job = await _create_job(
        db_session,
        dedupe_key="migration-old",
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        status=LongTermMemoryMutationStatus.FAILED,
        payload=payload,
    )
    old_job_id = old_job.id
    await memory_embedding_revision_crud.create(
        db_session,
        uid="user-a",
        revision=1,
        from_channel_id=1,
        from_model_id="embed-v1",
        from_dimensions=3,
        from_signature="sig-a",
        from_collection="collection-user-a",
        to_channel_id=2,
        to_model_id="embed-v2",
        to_dimensions=4,
        to_signature="sig-b",
        to_collection="collection-user-a-v2",
        job_id=old_job_id,
        status=LongTermMemoryEmbeddingRevisionStatus.FAILED,
        error="migration failed",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        migrations = _assert_standard(await client.get("/api/v1/memories/embedding-migrations"), 200)
        _assert_page(migrations, total=1)
        detail = _assert_standard(await client.get(f"/api/v1/memories/embedding-migrations/{old_job_id}"), 200)
        assert detail["data"]["job_id"] == old_job_id
        retried = _assert_standard(await client.post(f"/api/v1/memories/embedding-migrations/{old_job_id}/retry"), 200)
        new_job_id = retried["data"]["job"]["id"]
        new_detail = _assert_standard(await client.get(f"/api/v1/memories/embedding-migrations/{new_job_id}"), 200)
        assert new_detail["data"]["job_id"] == new_job_id
        cancelled = _assert_standard(await client.post(f"/api/v1/memories/embedding-migrations/{new_job_id}/cancel"), 200)
        assert cancelled["data"]["accepted"] is True
        assert cancelled["data"]["changed"] is True


@pytest.mark.asyncio
async def test_memory_collection_cleanup_retry_and_state_guard(api_app: tuple[FastAPI, SimpleNamespace], db_session: AsyncSession) -> None:
    app, _current_user = api_app
    store = await _create_store(
        db_session,
        old_collection_name="old-collection",
        old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.FAILED,
    )
    old_job = await _create_job(
        db_session,
        dedupe_key="cleanup-old",
        operation=LongTermMemoryMutationOperation.REINDEX,
        status=LongTermMemoryMutationStatus.FAILED,
        payload={
            "from": {
                "channel_id": 1,
                "model_id": "embed-v1",
                "dimensions": 3,
                "signature": "sig-a",
                "embedding_revision": 1,
                "collection": store.active_collection_name,
                "index_revision": 0,
            },
            "target": {"collection": "reindex-target", "index_revision": 1},
            "progress": {
                "phase": "preparing",
                "snapshot_initialized": False,
                "snapshot_boundary": 0,
                "cursor": 0,
                "total_count": 0,
                "success_count": 0,
                "failure_count": 0,
            },
        },
    )
    old_job_id = old_job.id
    store.old_collection_cleanup_job_id = old_job_id
    await db_session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        retried = _assert_standard(
            await client.post(
                f"/api/v1/memories/collections/{old_job_id}/cleanup-retry",
                json={"dedupe_key": "cleanup-retry"},
            ),
            200,
        )
        assert retried["data"]["created"] is True
        assert retried["data"]["job"]["operation"] == "reindex"
        blocked = await client.post(
            f"/api/v1/memories/collections/{old_job_id}/cleanup-retry",
            json={"dedupe_key": "cleanup-retry-2"},
        )
        _assert_standard(blocked, 409)


@pytest.mark.asyncio
async def test_memory_stage11_settings_api_exposes_organization_state_without_secrets(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    channel = await _create_chat_channel(db_session)
    await _create_store(
        db_session,
        organization_channel_id=None,
        organization_model_id=None,
    )
    record = await _create_record(
        db_session,
        memory_key="stage11-settings-memory",
        content="settings organization content",
        vector_item_id="stage11-settings-vector",
    )
    assert record.id is not None and channel.id is not None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        updated = _assert_standard(
            await client.post(
                "/api/v1/memories/settings",
                json={
                    "auto_organize_enabled": True,
                    "organization_channel_id": channel.id,
                    "organization_model_id": "stage11-chat-model",
                },
            ),
            200,
        )
        updated_data = updated["data"]
        assert updated_data["capacity"] == {
            "max_active_records": 50,
            "organize_trigger_records": 45,
            "content_max_tokens": 160,
            "active_record_count": 1,
            "status": "normal",
        }
        assert updated_data["organization"]["auto_organize_enabled"] is True
        assert updated_data["organization"]["model"]["channel_id"] == channel.id
        assert updated_data["organization"]["model"]["model_id"] == "stage11-chat-model"
        assert updated_data["organization"]["model"]["usage"] == "CHAT"
        assert updated_data["organization"]["current_job_id"] is None
        assert updated_data["organization"]["recent_job_id"] is None
        assert set(updated_data["blocking"]) == {"organize", "maintenance"}
        _assert_no_organization_secrets(updated_data)

        organize = _assert_standard(
            await client.post("/api/v1/memories/organize", json={"dedupe_key": "stage11-settings-organize"}),
            200,
        )
        organize_job_id = organize["data"]["job_id"]
        settings = _assert_standard(await client.get("/api/v1/memories/settings"), 200)
        settings_data = settings["data"]
        settings_update = _assert_standard(
            await client.post(
                "/api/v1/memories/settings",
                json={
                    "auto_organize_enabled": True,
                    "organization_channel_id": channel.id,
                    "organization_model_id": "stage11-chat-model",
                },
            ),
            200,
        )

    for payload in (settings_data, settings_update["data"], organize["data"]):
        _assert_no_organization_secrets(payload)
    for data in (settings_data, settings_update["data"]):
        assert data["capacity"]["max_active_records"] == 50
        assert data["capacity"]["organize_trigger_records"] == 45
        assert data["capacity"]["content_max_tokens"] == 160
        assert data["capacity"]["active_record_count"] == 1
        assert data["organization"]["current_job_id"] == organize_job_id
        assert data["organization"]["recent_job_id"] == organize_job_id
        assert data["organization"]["current_job"]["id"] == organize_job_id
        assert data["organization"]["recent_job"]["id"] == organize_job_id
        assert data["blocking"]["organize"]["blocked"] is True
        assert data["blocking"]["organize"]["reason"] == "organization_active"
        assert data["blocking"]["organize"]["job_id"] == organize_job_id
        assert data["blocking"]["maintenance"]["blocked"] is False


@pytest.mark.asyncio
async def test_memory_stage11_settings_api_forbids_internal_fields_and_limit_updates(
    api_app: tuple[FastAPI, SimpleNamespace],
) -> None:
    app, _current_user = api_app
    base_payload = {
        "auto_organize_enabled": False,
        "organization_channel_id": None,
        "organization_model_id": None,
    }
    forbidden_fields = {
        "uid": "user-b",
        "records": [],
        "session_id": "session-secret",
        "collection": "client-collection",
        "max_active_records": 999,
        "organize_trigger_records": 1,
        "content_max_tokens": 9999,
    }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for field, value in forbidden_fields.items():
            payload = {**base_payload, field: value}
            response = await client.post("/api/v1/memories/settings", json=payload)
            _assert_standard(response, 422)


@pytest.mark.asyncio
async def test_memory_stage11_organize_api_is_uid_scoped_idempotent_and_secret_free(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    channel = await _create_chat_channel(db_session)
    await _create_store(
        db_session,
        organization_channel_id=channel.id,
        organization_model_id="stage11-chat-model",
    )
    record = await _create_record(
        db_session,
        memory_key="stage11-organize-memory",
        content="organization source content",
        vector_item_id="stage11-organize-vector",
    )
    other_record = await _create_record(
        db_session,
        uid="user-b",
        memory_key="stage11-other-user-memory",
        content="other user content",
        vector_item_id="stage11-other-user-vector",
    )
    assert record.id is not None and other_record.id is not None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = _assert_standard(
            await client.post("/api/v1/memories/organize", json={"dedupe_key": "stage11-organize"}),
            200,
        )
        first_data = first["data"]
        assert first_data["job_id"] == first_data["job"]["id"]
        assert first_data["job"]["operation"] == "organize"
        assert first_data["created"] is True
        assert first_data["job"]["payload"]["snapshot"]["count"] == 1
        assert [item["memory_id"] for item in first_data["job"]["payload"]["snapshot"]["items"]] == [record.id]
        _assert_no_organization_secrets(first_data)

        duplicate = _assert_standard(
            await client.post("/api/v1/memories/organize", json={"dedupe_key": "stage11-organize"}),
            200,
        )
        assert duplicate["data"]["created"] is False
        assert duplicate["data"]["job_id"] == first_data["job_id"]
        _assert_no_organization_secrets(duplicate["data"])

        forbidden_fields = {
            "uid": "user-b",
            "records": [],
            "session_id": "session-secret",
            "collection": "client-collection",
            "max_attempts": 99,
        }
        for field, value in forbidden_fields.items():
            response = await client.post(
                "/api/v1/memories/organize",
                json={"dedupe_key": f"stage11-invalid-{field}", field: value},
            )
            _assert_standard(response, 422)


@pytest.mark.asyncio
async def test_memory_stage11_pin_and_unpin_api_persists_and_enforces_uid_scope(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    await _create_store(db_session)
    record = await _create_record(db_session, memory_key="stage11-pin-memory")
    assert record.id is not None
    record_id = int(record.id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        pinned = _assert_standard(await client.post(f"/api/v1/memories/{record_id}/pin"), 200)
        assert pinned["data"]["id"] == record_id
        assert pinned["data"]["pinned"] is True
        persisted = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=record_id)
        assert persisted is not None and persisted.pinned is True

        unpinned = _assert_standard(await client.post(f"/api/v1/memories/{record_id}/unpin"), 200)
        assert unpinned["data"]["pinned"] is False
        persisted = await memory_record_crud.get_by_id(db_session, uid="user-a", memory_id=record_id)
        assert persisted is not None and persisted.pinned is False

        current_user.uid = "user-b"
        _assert_standard(await client.post(f"/api/v1/memories/{record_id}/pin"), 404)
        _assert_standard(await client.post(f"/api/v1/memories/{record_id}/unpin"), 404)


@pytest.mark.asyncio
async def test_memory_stage11_job_summaries_are_top_level_and_secret_free(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    parent = await _create_job(
        db_session,
        dedupe_key="stage11-summary-parent",
        operation=LongTermMemoryMutationOperation.ORGANIZE,
        status=LongTermMemoryMutationStatus.SUCCEEDED,
        payload=_organization_job_payload(),
        result={
            "status": "succeeded",
            "snapshot_count": 2,
            "keep_count": 1,
            "update_count": 1,
            "merge_count": 0,
            "conflict_count": 0,
            "stale_count": 0,
            "skipped_count": 1,
            "child_job_ids": [],
            "budget": {"required_input_tokens": 300, "available_input_tokens": 500},
            "context_error": None,
        },
    )
    assert parent.id is not None
    child = await _create_job(
        db_session,
        dedupe_key="stage11-summary-child",
        operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
        status=LongTermMemoryMutationStatus.FAILED,
        parent_job_id=parent.id,
        payload={
            **_organization_job_payload(snapshot_count=2),
            "parent_job_id": parent.id,
        },
        result={
            "snapshot_count": 2,
            "keep_count": 0,
            "update_count": 0,
            "merge_count": 1,
            "conflict_count": 0,
            "stale_count": 1,
            "skipped_count": 0,
            "child_job_ids": [parent.id + 1],
            "budget": {"required_input_tokens": 301, "available_input_tokens": 499},
            "context_error": {"status": "organization_context_exceeded"},
        },
    )
    assert child.id is not None
    await memory_job_crud.update_status(
        db_session,
        uid="user-a",
        job_id=parent.id,
        status=LongTermMemoryMutationStatus.SUCCEEDED,
        result={
            "status": "succeeded",
            "snapshot_count": 2,
            "keep_count": 1,
            "update_count": 1,
            "merge_count": 0,
            "conflict_count": 0,
            "stale_count": 0,
            "skipped_count": 1,
            "child_job_ids": [child.id],
            "budget": {"required_input_tokens": 300, "available_input_tokens": 500},
            "context_error": None,
        },
    )

    expected_fields = {
        "parent_job_id",
        "snapshot_count",
        "keep_count",
        "update_count",
        "merge_count",
        "conflict_count",
        "stale_count",
        "skipped_count",
        "child_job_ids",
        "token_budget",
        "context_error",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        parent_detail = _assert_standard(await client.get(f"/api/v1/memories/jobs/{parent.id}"), 200)["data"]
        child_list = _assert_standard(await client.get("/api/v1/memories/jobs?operation=organize_merge"), 200)
        _assert_page(child_list, total=1)
        child_detail = child_list["data"]["items"][0]

    assert expected_fields.issubset(parent_detail)
    assert parent_detail["parent_job_id"] is None
    assert parent_detail["snapshot_count"] == 2
    assert parent_detail["child_job_ids"] == [child.id]
    assert parent_detail["token_budget"] == {"required_input_tokens": 300, "available_input_tokens": 500}
    assert expected_fields.issubset(child_detail)
    assert child_detail["parent_job_id"] == parent.id
    assert child_detail["merge_count"] == 1
    assert child_detail["stale_count"] == 1
    assert child_detail["context_error"] == {"status": "organization_context_exceeded"}
    _assert_no_organization_secrets(parent_detail)
    _assert_no_organization_secrets(child_detail)


@pytest.mark.asyncio
async def test_memory_stage11_retry_and_cancel_boundaries_are_exposed_by_api(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    channel = await _create_chat_channel(db_session)
    await _create_store(
        db_session,
        organization_channel_id=channel.id,
        organization_model_id="stage11-chat-model",
    )
    record = await _create_record(
        db_session,
        memory_key="stage11-retry-memory",
        content="current complete snapshot",
        vector_item_id="stage11-retry-vector",
    )
    failed_merge = await _create_job(
        db_session,
        dedupe_key="stage11-failed-merge",
        operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
        status=LongTermMemoryMutationStatus.FAILED,
        parent_job_id=999,
        payload={"parent_job_id": 999, "sources": []},
    )
    assert failed_merge.id is not None and record.id is not None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        retried = _assert_standard(await client.post(f"/api/v1/memories/jobs/{failed_merge.id}/retry"), 200)
        retry_data = retried["data"]
        assert retry_data["status"] == "accepted"
        assert retry_data["retry_scope"] == "new_snapshot"
        assert retry_data["job"]["operation"] == "organize"
        assert retry_data["job"]["payload"]["snapshot"]["count"] == 1
        assert retry_data["job"]["payload"]["snapshot"]["items"][0]["memory_id"] == record.id
        _assert_no_organization_secrets(retry_data)

        eviction_candidate = await _create_record(
            db_session,
            memory_key="stage11-old-eviction-candidate",
            content="old eviction candidate",
            vector_item_id="stage11-old-eviction-vector",
        )
        failed_eviction = await _create_job(
            db_session,
            dedupe_key="stage11-failed-eviction",
            operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            status=LongTermMemoryMutationStatus.FAILED,
            payload={
                "publication": _publication_payload(
                    key="stage11-retried-create",
                    content="retried create content",
                ),
                "candidate": {
                    "memory_id": eviction_candidate.id,
                    "version": eviction_candidate.version,
                    "vector_item_id": eviction_candidate.vector_item_id,
                    "record_snapshot": build_memory_record_snapshot(eviction_candidate),
                },
                "store": {"active_count": 2, "max_active_records": 50},
            },
        )
        assert failed_eviction.id is not None and eviction_candidate.id is not None
        retried_eviction = _assert_standard(await client.post(f"/api/v1/memories/jobs/{failed_eviction.id}/retry"), 200)
        retried_eviction_data = retried_eviction["data"]
        assert retried_eviction_data["status"] == "accepted"
        assert retried_eviction_data["job"]["id"] != failed_eviction.id
        assert retried_eviction_data["job"]["operation"] == LongTermMemoryMutationOperation.CREATE.value
        retried_candidate = await memory_record_crud.get_by_id(
            db_session,
            uid="user-a",
            memory_id=eviction_candidate.id,
        )
        assert retried_candidate is not None
        assert retried_candidate.is_active is True
        assert retried_candidate.deleted_at is None
        assert retried_candidate.pending_mutation_job_id is None

        candidate = await _create_record(
            db_session,
            memory_key="stage11-eviction-candidate",
            content="eviction candidate",
            vector_item_id="stage11-eviction-vector",
        )
        eviction_job = await _create_job(
            db_session,
            dedupe_key="stage11-pending-eviction",
            operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            status=LongTermMemoryMutationStatus.PENDING,
            payload={"candidate": {"memory_id": candidate.id}},
        )
        assert candidate.id is not None and eviction_job.id is not None
        candidate.pending_mutation_job_id = eviction_job.id
        db_session.add(candidate)
        await db_session.commit()

        cancelled_eviction = _assert_standard(await client.post(f"/api/v1/memories/jobs/{eviction_job.id}/cancel"), 200)
        assert cancelled_eviction["data"]["accepted"] is True
        assert cancelled_eviction["data"]["changed"] is True
        released_candidate = await memory_record_crud.get_by_id(
            db_session,
            uid="user-a",
            memory_id=candidate.id,
        )
        assert released_candidate is not None and released_candidate.pending_mutation_job_id is None

        cleanup_job = await _create_job(
            db_session,
            dedupe_key="stage11-pending-cleanup",
            operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
            status=LongTermMemoryMutationStatus.PENDING,
            memory_id=candidate.id,
            expected_version=candidate.version,
            payload={},
        )
        assert cleanup_job.id is not None
        cancelled_cleanup = _assert_standard(await client.post(f"/api/v1/memories/jobs/{cleanup_job.id}/cancel"), 200)
        assert cancelled_cleanup["data"]["accepted"] is False
        assert cancelled_cleanup["data"]["changed"] is False


@pytest.mark.asyncio
async def test_memory_stage11_auto_organize_source_is_exposed_in_list_get_and_history(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    content = "auto organize published content"
    recalled_at = datetime.now(UTC)
    record = await _create_record(
        db_session,
        memory_key="stage11-auto-organize-memory",
        content=content,
        source=LongTermMemorySource.AUTO_ORGANIZE,
        source_job_id=321,
        pinned=True,
        last_recalled_at=recalled_at,
    )
    assert record.id is not None
    await memory_revision_crud.create(
        db_session,
        uid="user-a",
        memory_id=record.id,
        version=record.version,
        memory_key=record.memory_key,
        memory_type=record.memory_type,
        content=content,
        content_token_count=estimate_tokens(normalize_memory_content(content)),
        content_hash=build_memory_content_hash(content),
        source=LongTermMemorySource.AUTO_ORGANIZE,
        source_job_id=321,
    )

    expected_tokens = estimate_tokens(normalize_memory_content(content))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        listed = _assert_standard(await client.get("/api/v1/memories/list?size=20"), 200)
        _assert_page(listed, total=1)
        listed_record = listed["data"]["items"][0]
        detail = _assert_standard(await client.get(f"/api/v1/memories/get?memory_id={record.id}"), 200)["data"]
        history = _assert_standard(await client.get(f"/api/v1/memories/{record.id}/history"), 200)
        history_record = history["data"]["items"][0]

    assert listed_record["source"] == LongTermMemorySource.AUTO_ORGANIZE.value
    assert listed_record["content_token_count"] == expected_tokens
    assert listed_record["pinned"] is True
    assert listed_record["last_recalled_at"] is not None
    assert detail["source"] == LongTermMemorySource.AUTO_ORGANIZE.value
    assert detail["content_token_count"] == expected_tokens
    assert detail["pinned"] is True
    assert detail["last_recalled_at"] is not None
    assert history_record["source"] == LongTermMemorySource.AUTO_ORGANIZE.value
    assert history_record["content_token_count"] == expected_tokens
