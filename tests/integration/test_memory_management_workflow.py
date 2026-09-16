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

import app.core.crypto as crypto_module
from app.api.v1.memories import router
from app.core.crud.channel.channel import channel_crud
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import (
    memory_embedding_revision_crud,
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.memory.normalization import build_memory_content_hash, normalize_memory_content
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

ORGANIZATION_API_KEY = "organization-api-key"
ORGANIZATION_BASE_URL = "https://llm.example/v1"
ORGANIZATION_HTTP_PROXY = "http://proxy.example:8080"
ORGANIZATION_HEADER_VALUE = "secret-header"


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"\x00" * 32)


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


def _chat_model(model_id: str = "organization-chat-model", **overrides: object) -> dict:
    model = {
        "model_id": model_id,
        "usage": "CHAT",
        "protocol": "OPENAI",
        "context_window_k": 64,
        "max_tokens": 20_000,
        "temperature": 0.25,
        "top_p": 0.8,
        "is_enabled": True,
        "description": "organization model",
        "advanced_settings": {"custom_headers": {"x-secret": ORGANIZATION_HEADER_VALUE}},
    }
    model.update(overrides)
    return model


async def _create_chat_channel(
    db: AsyncSession,
    *,
    name: str | None = None,
    model_id: str = "organization-chat-model",
    api_key: str = ORGANIZATION_API_KEY,
    base_url: str = ORGANIZATION_BASE_URL,
    http_proxy: str = ORGANIZATION_HTTP_PROXY,
    model_ids: list[dict] | None = None,
) -> ModelChannel:
    return await channel_crud.create_with_plain_api_key(
        db,
        obj_in=ChannelCreate(
            name=name or f"chat-channel-{uuid4().hex[:8]}",
            api_key=api_key,
            base_url=base_url,
            http_proxy=http_proxy,
            is_active=True,
            model_ids=model_ids or [_chat_model(model_id=model_id)],
        ),
    )


def _organization_job_payload(*, snapshot_count: int = 2, channel_id: int = 1, model_id: str = "organization-chat-model") -> dict:
    return {
        "trigger": "manual",
        "snapshot": {"count": snapshot_count},
        "organization_model": {
            "channel_id": channel_id,
            "channel_name": "secret-channel",
            "model_id": model_id,
            "usage": "CHAT",
            "protocol": "openai",
            "base_url": ORGANIZATION_BASE_URL,
            "api_key": ORGANIZATION_API_KEY,
            "http_proxy": ORGANIZATION_HTTP_PROXY,
            "custom_headers": {"x-secret": ORGANIZATION_HEADER_VALUE},
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


def _organization_merge_payload(*, parent_job_id: int, action: str, source_ids: list[int]) -> dict:
    return {
        "parent_job_id": parent_job_id,
        "snapshot_digest": "a" * 64,
        "active_embedding_revision": 1,
        "index_revision": 1,
        "policy_version": 1,
        "action": action,
        "sources": [{"memory_id": memory_id, "expected_version": 1, "pinned": False} for memory_id in source_ids],
        "primary_memory_id": source_ids[0],
        "target": {
            "content": "organized memory content",
            "memory_key": "organized-memory",
            "memory_type": "fact",
            "content_token_count": 3,
            "content_hash": "b" * 64,
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
async def test_memory_management_queries_filter_page_and_isolate_uid_through_api(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    await _create_store(db_session, "user-a")
    await _create_store(db_session, "user-b")

    recalled_at = datetime.now(UTC)
    low = await _create_record(
        db_session,
        uid="user-a",
        memory_key="needle-low",
        content="needle low",
        version=1,
        memory_type=LongTermMemoryType.FACT,
        source=LongTermMemorySource.AUTO_ORGANIZE,
        source_job_id=321,
        pinned=True,
        last_recalled_at=recalled_at,
    )
    await _create_record(
        db_session,
        uid="user-a",
        memory_key="needle-todo",
        content="needle todo",
        version=2,
        memory_type=LongTermMemoryType.TODO,
    )
    high = await _create_record(
        db_session,
        uid="user-a",
        memory_key="needle-high",
        content="needle high",
        version=3,
        memory_type=LongTermMemoryType.FACT,
    )
    await _create_record(
        db_session,
        uid="user-b",
        memory_key="needle-foreign",
        content="needle foreign",
        version=4,
        memory_type=LongTermMemoryType.FACT,
    )
    assert low.id is not None and high.id is not None

    owner_failed = await _create_job(
        db_session,
        uid="user-a",
        dedupe_key="query-owner-failed",
        operation=LongTermMemoryMutationOperation.CREATE,
        status=LongTermMemoryMutationStatus.FAILED,
    )
    owner_pending = await _create_job(
        db_session,
        uid="user-a",
        dedupe_key="query-owner-pending",
        operation=LongTermMemoryMutationOperation.CREATE,
        status=LongTermMemoryMutationStatus.PENDING,
        payload={"progress": {"success_count": 2, "total_count": 5}},
    )
    await _create_job(
        db_session,
        uid="user-a",
        dedupe_key="query-owner-later-pending",
        operation=LongTermMemoryMutationOperation.UPDATE,
        status=LongTermMemoryMutationStatus.PENDING,
    )
    await _create_job(
        db_session,
        uid="user-b",
        dedupe_key="query-foreign-failed",
        operation=LongTermMemoryMutationOperation.CREATE,
        status=LongTermMemoryMutationStatus.FAILED,
    )

    for version in (1, 2, 3):
        await memory_revision_crud.create(
            db_session,
            uid="user-a",
            memory_id=low.id,
            version=version,
            memory_key="needle-low",
            memory_type=LongTermMemoryType.FACT,
            content=f"history-{version}",
            content_token_count=estimate_tokens(f"history-{version}"),
            content_hash=build_memory_content_hash(f"history-{version}"),
            source=LongTermMemorySource.AUTO_ORGANIZE if version == 2 else LongTermMemorySource.USER_API,
            source_job_id=321 if version == 2 else None,
        )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        memories = _assert_standard(
            await client.get(
                "/api/v1/memories/list",
                params={
                    "page": 2,
                    "size": 1,
                    "keyword": "needle",
                    "memory_type": LongTermMemoryType.FACT.value,
                    "sort_by": "version",
                    "sort_order": "desc",
                },
            ),
            200,
        )
        _assert_page(memories, total=2, page=2, size=1)
        assert [item["id"] for item in memories["data"]["items"]] == [low.id]
        listed_low = memories["data"]["items"][0]
        assert listed_low["source"] == LongTermMemorySource.AUTO_ORGANIZE.value
        assert listed_low["pinned"] is True
        assert listed_low["last_recalled_at"] is not None
        detail = _assert_standard(
            await client.get("/api/v1/memories/get", params={"memory_id": low.id}),
            200,
        )["data"]
        assert detail["source"] == LongTermMemorySource.AUTO_ORGANIZE.value
        assert detail["pinned"] is True
        assert detail["last_recalled_at"] is not None

        jobs = _assert_standard(
            await client.get(
                "/api/v1/memories/jobs",
                params={
                    "page": 1,
                    "size": 1,
                    "status": LongTermMemoryMutationStatus.FAILED.value,
                    "operation": LongTermMemoryMutationOperation.CREATE.value,
                },
            ),
            200,
        )
        _assert_page(jobs, total=1, page=1, size=1)
        assert [item["id"] for item in jobs["data"]["items"]] == [owner_failed.id]

        settings = _assert_standard(await client.get("/api/v1/memories/settings"), 200)
        current_job = settings["data"]["current_job"]
        assert current_job is not None
        assert current_job["id"] == owner_pending.id
        assert current_job["payload"]["progress"] == {"success_count": 2, "total_count": 5}

        history = _assert_standard(
            await client.get(f"/api/v1/memories/{low.id}/history", params={"page": 2, "size": 1}),
            200,
        )
        _assert_page(history, total=3, page=2, size=1)
        history_item = history["data"]["items"][0]
        assert history_item["version"] == 2
        assert history_item["source"] == LongTermMemorySource.AUTO_ORGANIZE.value
        assert history_item["source_job_id"] == 321
        restore = await client.post(
            f"/api/v1/memories/{low.id}/restore",
            json={"revision_version": 1, "expected_version": 1},
        )
        _assert_standard(restore, 404)

        _assert_standard(await client.get("/api/v1/memories/list?page=0"), 422)
        _assert_standard(await client.get("/api/v1/memories/list?sort_by=invalid"), 422)
        _assert_standard(await client.get("/api/v1/memories/jobs/0"), 422)


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
async def test_memory_maintenance_workflow_reindexes_migrates_and_retries_collection_cleanup(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    await _create_store(db_session)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        reindex = _assert_standard(
            await client.post("/api/v1/memories/reindex", json={"dedupe_key": "maintenance-reindex"}),
            200,
        )
        reindex_job_id = reindex["data"]["job"]["id"]
        assert reindex["data"]["created"] is True
        assert reindex["data"]["job"]["operation"] == LongTermMemoryMutationOperation.REINDEX.value

        duplicate_reindex = _assert_standard(
            await client.post("/api/v1/memories/reindex", json={"dedupe_key": "maintenance-reindex"}),
            200,
        )
        assert duplicate_reindex["data"]["created"] is False
        assert duplicate_reindex["data"]["job"]["id"] == reindex_job_id

        blocked_reindex = await client.post(
            "/api/v1/memories/reindex",
            json={"dedupe_key": "maintenance-reindex-blocked"},
        )
        _assert_standard(blocked_reindex, 409)

    reindex_job = await memory_job_crud.get_by_id(db_session, uid="user-a", job_id=reindex_job_id)
    store = await memory_store_crud.get_by_uid(db_session, uid="user-a")
    assert reindex_job is not None
    assert store is not None
    reindex_job.status = LongTermMemoryMutationStatus.FAILED
    reindex_job.active_mutation_key = None
    reindex_job.error = "reindex failed"
    reindex_job.finished_at = datetime.now(UTC)
    store.index_status = LongTermMemoryIndexStatus.READY
    await db_session.commit()

    migration_job = await _create_job(
        db_session,
        dedupe_key="maintenance-migration-failed",
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        status=LongTermMemoryMutationStatus.FAILED,
        payload=_migration_payload(),
    )
    migration_job_id = migration_job.id
    assert migration_job_id is not None
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
        job_id=migration_job_id,
        status=LongTermMemoryEmbeddingRevisionStatus.FAILED,
        error="migration failed",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        migrations = _assert_standard(await client.get("/api/v1/memories/embedding-migrations"), 200)
        _assert_page(migrations, total=1)
        assert migrations["data"]["items"][0]["job_id"] == migration_job_id

        detail = _assert_standard(
            await client.get(f"/api/v1/memories/embedding-migrations/{migration_job_id}"),
            200,
        )
        assert detail["data"]["job_id"] == migration_job_id

        retried = _assert_standard(
            await client.post(f"/api/v1/memories/embedding-migrations/{migration_job_id}/retry"),
            200,
        )
        retry_job_id = retried["data"]["job"]["id"]
        assert retry_job_id != migration_job_id
        assert retried["data"]["job"]["operation"] == LongTermMemoryMutationOperation.EMBEDDING_MIGRATION.value

        retry_detail = _assert_standard(
            await client.get(f"/api/v1/memories/embedding-migrations/{retry_job_id}"),
            200,
        )
        assert retry_detail["data"]["job_id"] == retry_job_id

        cancelled = _assert_standard(
            await client.post(f"/api/v1/memories/embedding-migrations/{retry_job_id}/cancel"),
            200,
        )
        assert cancelled["data"]["accepted"] is True
        assert cancelled["data"]["changed"] is True

    store = await memory_store_crud.get_by_uid(db_session, uid="user-a")
    assert store is not None
    store.old_collection_name = "maintenance-old-collection"
    store.old_collection_cleanup_status = LongTermMemoryOldCollectionCleanupStatus.FAILED
    store.old_collection_cleanup_job_id = reindex_job_id
    await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        invalid_cleanup_retry = await client.post(
            "/api/v1/memories/collections/999999/cleanup-retry",
            json={"dedupe_key": "maintenance-cleanup-invalid"},
        )
        _assert_standard(invalid_cleanup_retry, 409)
        assert await memory_job_crud.get_by_dedupe_key(
            db_session,
            uid="user-a",
            dedupe_key="maintenance-cleanup-invalid",
        ) is None

        cleanup_retry = _assert_standard(
            await client.post(
                f"/api/v1/memories/collections/{reindex_job_id}/cleanup-retry",
                json={"dedupe_key": "maintenance-cleanup-retry"},
            ),
            200,
        )
        assert cleanup_retry["data"]["created"] is True
        assert cleanup_retry["data"]["job"]["operation"] == LongTermMemoryMutationOperation.REINDEX.value

        blocked_cleanup_retry = await client.post(
            f"/api/v1/memories/collections/{reindex_job_id}/cleanup-retry",
            json={"dedupe_key": "maintenance-cleanup-retry-blocked"},
        )
        _assert_standard(blocked_cleanup_retry, 409)


@pytest.mark.asyncio
async def test_memory_organization_workflow_configures_submits_idempotently_and_exposes_blocking_state(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    channel = await _create_chat_channel(db_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        unconfigured = _assert_standard(await client.get("/api/v1/memories/settings"), 200)
    assert unconfigured["data"]["configured"] is False
    assert unconfigured["data"]["capacity"] == {
        "max_active_records": 50,
        "organize_trigger_records": 45,
        "content_max_tokens": 160,
        "active_record_count": 0,
        "status": "normal",
    }
    assert unconfigured["data"]["store"] == {
        "content_max_tokens": 160,
        "active_record_count": 0,
    }

    await _create_store(
        db_session,
        organization_channel_id=None,
        organization_model_id=None,
    )
    record = await _create_record(
        db_session,
        memory_key="settings-memory",
        content="settings organization content",
        vector_item_id="settings-vector",
    )
    other_record = await _create_record(
        db_session,
        uid="user-b",
        memory_key="organization-other-user-memory",
        content="other user organization content",
        vector_item_id="organization-other-user-vector",
    )
    assert record.id is not None and other_record.id is not None and channel.id is not None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        updated = _assert_standard(
            await client.post(
                "/api/v1/memories/settings",
                json={
                    "auto_organize_enabled": True,
                    "organization_channel_id": channel.id,
                    "organization_model_id": "organization-chat-model",
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
        assert updated_data["organization"]["model"]["model_id"] == "organization-chat-model"
        assert updated_data["organization"]["model"]["usage"] == "CHAT"
        assert updated_data["organization"]["current_job_id"] is None
        assert updated_data["organization"]["recent_job_id"] is None
        assert updated_data["current_job"] is None
        assert set(updated_data["blocking"]) == {"organize", "maintenance"}
        _assert_no_organization_secrets(updated_data)

        organize = _assert_standard(
            await client.post("/api/v1/memories/organize", json={"dedupe_key": "organization-settings-organize"}),
            200,
        )
        organize_job_id = organize["data"]["job_id"]
        assert organize["data"]["created"] is True
        assert organize["data"]["job"]["payload"]["snapshot"]["count"] == 1
        assert [item["memory_id"] for item in organize["data"]["job"]["payload"]["snapshot"]["items"]] == [record.id]

        duplicate = _assert_standard(
            await client.post(
                "/api/v1/memories/organize",
                json={"dedupe_key": "organization-settings-organize"},
            ),
            200,
        )
        assert duplicate["data"]["created"] is False
        assert duplicate["data"]["job_id"] == organize_job_id
        _assert_no_organization_secrets(duplicate["data"])

        settings = _assert_standard(await client.get("/api/v1/memories/settings"), 200)
        settings_data = settings["data"]
        settings_update = _assert_standard(
            await client.post(
                "/api/v1/memories/settings",
                json={
                    "auto_organize_enabled": True,
                    "organization_channel_id": channel.id,
                    "organization_model_id": "organization-chat-model",
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
        assert data["current_job"]["id"] == organize_job_id
        assert data["organization"]["current_job_id"] == organize_job_id
        assert data["organization"]["recent_job_id"] == organize_job_id
        assert data["organization"]["current_job"]["id"] == organize_job_id
        assert data["organization"]["recent_job"]["id"] == organize_job_id
        assert data["blocking"]["organize"]["blocked"] is True
        assert data["blocking"]["organize"]["reason"] == "organization_active"
        assert data["blocking"]["organize"]["job_id"] == organize_job_id
        assert data["blocking"]["maintenance"]["blocked"] is True
        assert data["blocking"]["maintenance"]["reason"] == "organization_active"
        assert data["blocking"]["maintenance"]["job_id"] == organize_job_id
