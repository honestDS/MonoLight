from collections.abc import AsyncGenerator
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.api.v1.memories import router
from app.core.crud.memory import (
    memory_embedding_revision_crud,
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.memory.normalization import build_memory_content_hash
from app.core.security import get_current_user
from app.handler import register_handlers
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
    LongTermMemoryStore.__table__,
    LongTermMemoryEmbeddingRevision.__table__,
    LongTermMemoryEmbeddingDelta.__table__,
    LongTermMemoryRecord.__table__,
    LongTermMemoryRevision.__table__,
    LongTermMemoryMutationJob.__table__,
]


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
        "importance": 5,
        "scope": "project",
        "content": content,
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
        "importance": 5,
        "scope": None,
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
                "importance": 7,
                "scope": "project",
                "change_evidence": "user request",
            },
        )
        create_payload = _assert_standard(create_response, 200)
        assert create_payload["data"]["status"] == "accepted"
        assert create_payload["data"]["job"]["operation"] == "create"
        assert "uid" not in create_payload["data"]["job"]

        list_payload = _assert_standard(await client.get("/api/v1/memories/list?size=20"), 200)
        _assert_page(list_payload, total=2)
        get_payload = _assert_standard(await client.get(f"/api/v1/memories/get?memory_id={first_id}"), 200)
        assert get_payload["data"]["id"] == first_id

        update_response = await client.post(
            "/api/v1/memories/update",
            json={
                "memory_id": first_id,
                "expected_version": 1,
                "dedupe_key": "api-update",
                "content": "updated content",
                "memory_key": "first-updated",
                "memory_type": "fact",
                "importance": 8,
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
        importance=5,
        content="content-a",
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
                    "importance": 1,
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
@pytest.mark.parametrize("extra_field", ["uid", "source", "source_message_id", "collection", "dimensions"])
async def test_memory_api_forbids_internal_and_embedding_fields(api_app: tuple[FastAPI, SimpleNamespace], extra_field: str) -> None:
    app, _current_user = api_app
    body = {
        "content": "content",
        "memory_key": "key",
        "memory_type": "fact",
        "importance": 1,
        extra_field: 1 if extra_field in {"source_message_id", "dimensions"} else "forbidden",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        payload = _assert_standard(await client.post("/api/v1/memories/create", json=body), 422)
    assert payload["data"] is None


@pytest.mark.asyncio
async def test_memory_api_validates_fields_versions_and_state_transitions(api_app: tuple[FastAPI, SimpleNamespace], db_session: AsyncSession) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    record = await _create_record(db_session)
    pending_job = await _create_job(db_session, dedupe_key="pending-job")
    record_id, pending_job_id = record.id, pending_job.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        bad_importance = await client.post(
            "/api/v1/memories/create",
            json={"content": "x", "memory_key": "x", "memory_type": "fact", "importance": 11},
        )
        _assert_standard(bad_importance, 422)
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
                "importance": 1,
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
                    "importance": 1,
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
            importance=5,
            content=content,
            content_hash=build_memory_content_hash(content),
            source=LongTermMemorySource.USER_API,
        )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        history = _assert_standard(await client.get(f"/api/v1/memories/{record_id}/history?size=1"), 200)
        _assert_page(history, total=2, size=1)
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
        importance=record.importance,
        scope=record.scope,
        content=record.content,
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
async def test_memory_settings_and_reindex_api_with_state_guard(api_app: tuple[FastAPI, SimpleNamespace], db_session: AsyncSession) -> None:
    app, _current_user = api_app
    await _create_store(db_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        settings = _assert_standard(await client.get("/api/v1/memories/settings"), 200)
        assert settings["data"]["configured"] is True
        assert settings["data"]["active"]["revision"] == 1
        reindex = _assert_standard(await client.post("/api/v1/memories/reindex", json={"dedupe_key": "reindex-a"}), 200)
        assert reindex["data"]["created"] is True
        assert reindex["data"]["job"]["operation"] == "reindex"
        blocked = await client.post("/api/v1/memories/reindex", json={"dedupe_key": "reindex-b"})
        _assert_standard(blocked, 409)


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
