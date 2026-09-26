import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

import app.core.memory_jobs.handler_cleanup as memory_cleanup_handler
import app.core.memory_jobs.handler_execution as memory_execution_handler
import app.core.memory_jobs.handler_prepare as memory_prepare_handler
import app.core.memory_jobs.handler_publication_validation as memory_publication_validation_handler
import app.core.memory_jobs.handler_vector as memory_vector_handler
import app.core.memory_jobs.vector_cleanup as memory_vector_cleanup
from app.api.v1.memories import router
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_store_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.memory_jobs import handlers as memory_handlers
from app.core.memory_jobs.consumer import MemoryJobConsumer
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryIndexStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryRevision,
    LongTermMemoryStore,
)
from app.providers.database import get_db
from tests.database_support import clone_sqlite_schema

MEMORY_TABLES = [
    ModelChannel.__table__,
    LongTermMemoryStore.__table__,
    LongTermMemoryEmbeddingRevision.__table__,
    LongTermMemoryEmbeddingDelta.__table__,
    LongTermMemoryRecord.__table__,
    LongTermMemoryRevision.__table__,
    LongTermMemoryMutationJob.__table__,
]


class MemoryVectorBackend:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}
        self.deleted_items: list[tuple[str, tuple[str, ...]]] = []
        self.delete_error: BaseException | None = None

    async def load_config(
        self,
        _db: AsyncSession,
        channel_id: int,
        model_id: str,
        **_kwargs: Any,
    ) -> EmbeddingRuntimeConfig:
        assert channel_id == 1
        assert model_id == "embed-v1"
        return EmbeddingRuntimeConfig(
            channel_id=1,
            channel_name="memory-embedding",
            model_id="embed-v1",
            declared_dimensions=3,
            protocol="openai_embedding",
            timeout=30.0,
            base_url="https://embedding.invalid/v1",
            api_key="test-key",
        )

    async def embed(
        self,
        _config: EmbeddingRuntimeConfig,
        texts: list[str],
        **_kwargs: Any,
    ) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _text in texts]

    async def get_or_create_collection(
        self,
        collection_name: str,
        **_kwargs: Any,
    ) -> dict[str, dict[str, Any]]:
        return self.collections.setdefault(collection_name, {})

    async def upsert(
        self,
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> int:
        collection = self.collections.setdefault(collection_name, {})
        for item_id, document, embedding, metadata in zip(
            item_ids,
            documents,
            embeddings,
            metadatas,
            strict=True,
        ):
            collection[item_id] = {
                "document": document,
                "embedding": list(embedding),
                "metadata": dict(metadata),
            }
        return len(item_ids)

    async def validate(self, collection_name: str) -> SimpleNamespace:
        return SimpleNamespace(exists=collection_name in self.collections)

    async def delete(
        self,
        collection_name: str,
        item_ids: list[str],
        **_kwargs: Any,
    ) -> int:
        self.deleted_items.append((collection_name, tuple(item_ids)))
        if self.delete_error is not None:
            raise self.delete_error
        collection = self.collections.get(collection_name)
        if collection is not None:
            for item_id in item_ids:
                collection.pop(item_id, None)
        return len(item_ids)


@pytest_asyncio.fixture
async def memory_lifecycle_runtime(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], FastAPI, MemoryJobConsumer, MemoryVectorBackend]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'memory-record-lifecycle.db'}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    await clone_sqlite_schema(tmp_path / "memory-record-lifecycle.db", tables=MEMORY_TABLES)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    backend = MemoryVectorBackend()
    monkeypatch.setattr(memory_prepare_handler, "load_embedding_runtime_config", backend.load_config)
    monkeypatch.setattr(
        memory_publication_validation_handler,
        "load_embedding_runtime_config",
        backend.load_config,
    )
    monkeypatch.setattr(memory_execution_handler, "embed_texts_with_config", backend.embed)
    monkeypatch.setattr(
        memory_execution_handler,
        "async_get_or_create_collection",
        backend.get_or_create_collection,
    )
    monkeypatch.setattr(memory_execution_handler, "async_upsert_collection_items", backend.upsert)
    monkeypatch.setattr(memory_cleanup_handler, "async_validate_collection", backend.validate)
    monkeypatch.setattr(memory_cleanup_handler, "async_delete_collection_items", backend.delete)
    monkeypatch.setattr(memory_vector_handler, "async_validate_collection", backend.validate)
    monkeypatch.setattr(memory_vector_handler, "async_delete_collection_items", backend.delete)
    monkeypatch.setattr(memory_vector_cleanup, "async_validate_collection", backend.validate)
    monkeypatch.setattr(memory_vector_cleanup, "async_delete_collection_items", backend.delete)

    async with session_factory() as db:
        db.add(
            ModelChannel(
                id=1,
                name="memory-embedding-channel",
                api_key="enc:v1:test-key",
                base_url="https://embedding.invalid/v1",
                is_active=True,
                model_ids=[
                    {
                        "model_id": "embed-v1",
                        "usage": "EMBEDDING",
                        "protocol": "OPENAI_EMBEDDING",
                        "is_enabled": True,
                        "embedding_dimensions": 3,
                    }
                ],
            )
        )
        await db.commit()
        await memory_store_crud.create(
            db,
            uid="memory-user",
            active_embedding_channel_id=1,
            active_embedding_model_id="embed-v1",
            active_embedding_dimensions=3,
            active_embedding_signature="memory-signature-v1",
            active_embedding_revision=1,
            active_collection_name="memory-user-active",
            index_status=LongTermMemoryIndexStatus.READY,
        )
        await memory_store_crud.create(
            db,
            uid="other-user",
            active_embedding_channel_id=1,
            active_embedding_model_id="embed-v1",
            active_embedding_dimensions=3,
            active_embedding_signature="memory-signature-v1",
            active_embedding_revision=1,
            active_collection_name="other-user-active",
            index_status=LongTermMemoryIndexStatus.READY,
        )

    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        async with session_factory() as db:
            yield db

    current_user = SimpleNamespace(uid="memory-user", is_superuser=False)

    def override_get_current_user():
        return current_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    executor = memory_handlers.create_default_memory_job_executor(session_factory=session_factory)
    consumer = MemoryJobConsumer(
        executor,
        session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=30,
        renew_interval_seconds=10,
        recovery_interval_seconds=1_000_000,
        max_concurrency=1,
        recovery_retry_delay_seconds=1,
        shutdown_retry_delay_seconds=0.01,
    )
    try:
        yield session_factory, app, consumer, backend, current_user
    finally:
        await consumer.stop()
        await engine.dispose()


async def _run_job_to_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    consumer: MemoryJobConsumer,
    *,
    job_id: int,
) -> LongTermMemoryMutationJob:
    deadline = asyncio.get_running_loop().time() + 3.0
    while True:
        async with session_factory() as db:
            job = await memory_job_crud.get_by_id(
                db,
                uid="memory-user",
                job_id=job_id,
            )
        assert job is not None
        if job.status in {
            LongTermMemoryMutationStatus.SUCCEEDED,
            LongTermMemoryMutationStatus.FAILED,
            LongTermMemoryMutationStatus.CANCELLED,
        }:
            return job
        await consumer.run_once()
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"memory job {job_id} did not finish")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_manual_memory_lifecycle_runs_api_jobs_worker_versions_pin_history_and_delete_cleanup(
    memory_lifecycle_runtime: tuple[
        async_sessionmaker[AsyncSession],
        FastAPI,
        MemoryJobConsumer,
        MemoryVectorBackend,
        SimpleNamespace,
    ],
) -> None:
    session_factory, app, consumer, backend, current_user = memory_lifecycle_runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/memories/create",
            json={
                "dedupe_key": "lifecycle-create",
                "content": "The preferred editor is VS Code.",
                "memory_key": "preferred-editor",
                "memory_type": "preference",
                "change_evidence": "user preference",
            },
        )
        assert created.status_code == 200
        create_data = created.json()["data"]
        create_job_id = create_data["job"]["id"]
        assert create_data["status"] == "accepted"

        create_job = await _run_job_to_terminal(
            session_factory,
            consumer,
            job_id=create_job_id,
        )
        assert create_job.status == LongTermMemoryMutationStatus.SUCCEEDED
        assert create_job.memory_id is not None
        memory_id = create_job.memory_id

        listed = await client.get(
            "/api/v1/memories/list",
            params={"keyword": "VS Code", "memory_type": "preference"},
        )
        assert listed.status_code == 200
        listed_items = listed.json()["data"]["items"]
        assert [item["id"] for item in listed_items] == [memory_id]
        assert listed_items[0]["version"] == 1
        assert listed_items[0]["pinned"] is False

        detail = await client.get(
            "/api/v1/memories/get",
            params={"memory_id": memory_id},
        )
        assert detail.status_code == 200
        assert detail.json()["data"]["content"] == "The preferred editor is VS Code."

        pinned = await client.post(f"/api/v1/memories/{memory_id}/pin")
        assert pinned.status_code == 200
        assert pinned.json()["data"]["pinned"] is True
        pinned_again = await client.post(f"/api/v1/memories/{memory_id}/pin")
        assert pinned_again.status_code == 200
        assert pinned_again.json()["data"]["pinned"] is True
        unpinned = await client.post(f"/api/v1/memories/{memory_id}/unpin")
        assert unpinned.status_code == 200
        assert unpinned.json()["data"]["pinned"] is False
        unpinned_again = await client.post(f"/api/v1/memories/{memory_id}/unpin")
        assert unpinned_again.status_code == 200
        assert unpinned_again.json()["data"]["pinned"] is False

        current_user.uid = "other-user"
        hidden_list = await client.get("/api/v1/memories/list")
        assert hidden_list.status_code == 200
        assert hidden_list.json()["data"]["total"] == 0
        for inaccessible in (
            await client.get("/api/v1/memories/get", params={"memory_id": memory_id}),
            await client.get(f"/api/v1/memories/{memory_id}/history"),
            await client.get(f"/api/v1/memories/jobs/{create_job_id}"),
            await client.post(f"/api/v1/memories/{memory_id}/pin"),
            await client.post(f"/api/v1/memories/{memory_id}/unpin"),
            await client.post(
                "/api/v1/memories/update",
                json={
                    "memory_id": memory_id,
                    "expected_version": 1,
                    "content": "cross-user update",
                    "memory_key": "cross-user",
                    "memory_type": "fact",
                },
            ),
            await client.post(
                "/api/v1/memories/delete",
                json={"memory_id": memory_id, "expected_version": 1},
            ),
            await client.post(
                f"/api/v1/memories/{memory_id}/restore",
                json={"revision_version": 1, "expected_version": 1},
            ),
            await client.post(
                f"/api/v1/memories/{memory_id}/resume-current",
                json={"expected_version": 1},
            ),
        ):
            assert inaccessible.status_code == 404
        current_user.uid = "memory-user"

        updated = await client.post(
            "/api/v1/memories/update",
            json={
                "memory_id": memory_id,
                "expected_version": 1,
                "dedupe_key": "lifecycle-update",
                "content": "The preferred editor is Visual Studio Code.",
                "memory_key": "preferred-editor",
                "memory_type": "preference",
                "change_evidence": "user clarified editor name",
            },
        )
        assert updated.status_code == 200
        update_job_id = updated.json()["data"]["job"]["id"]
        blocked_pin = await client.post(f"/api/v1/memories/{memory_id}/pin")
        blocked_unpin = await client.post(f"/api/v1/memories/{memory_id}/unpin")
        assert blocked_pin.status_code == 409
        assert blocked_unpin.status_code == 409
        update_job = await _run_job_to_terminal(
            session_factory,
            consumer,
            job_id=update_job_id,
        )
        assert update_job.status == LongTermMemoryMutationStatus.SUCCEEDED

        updated_detail = await client.get(
            "/api/v1/memories/get",
            params={"memory_id": memory_id},
        )
        assert updated_detail.status_code == 200
        updated_data = updated_detail.json()["data"]
        assert updated_data["version"] == 2
        assert updated_data["content"] == "The preferred editor is Visual Studio Code."

        history = await client.get(f"/api/v1/memories/{memory_id}/history")
        assert history.status_code == 200
        history_items = history.json()["data"]["items"]
        assert [item["version"] for item in history_items] == [2, 1]
        assert [item["content"] for item in history_items] == [
            "The preferred editor is Visual Studio Code.",
            "The preferred editor is VS Code.",
        ]

        stale_delete = await client.post(
            "/api/v1/memories/delete",
            json={
                "memory_id": memory_id,
                "expected_version": 1,
                "dedupe_key": "lifecycle-stale-delete",
            },
        )
        assert stale_delete.status_code == 409

        deleted = await client.post(
            "/api/v1/memories/delete",
            json={
                "memory_id": memory_id,
                "expected_version": 2,
                "dedupe_key": "lifecycle-delete",
            },
        )
        assert deleted.status_code == 200
        delete_job_id = deleted.json()["data"]["job"]["id"]
        delete_job = await _run_job_to_terminal(
            session_factory,
            consumer,
            job_id=delete_job_id,
        )
        assert delete_job.status == LongTermMemoryMutationStatus.SUCCEEDED

        missing = await client.get(
            "/api/v1/memories/get",
            params={"memory_id": memory_id},
        )
        assert missing.status_code == 404
        history_after_delete = await client.get(f"/api/v1/memories/{memory_id}/history")
        assert history_after_delete.status_code == 200
        assert [item["version"] for item in history_after_delete.json()["data"]["items"]] == [2, 1]

    async with session_factory() as db:
        current_record = await db.get(LongTermMemoryRecord, memory_id)
        revisions = list((await db.execute(select(LongTermMemoryRevision).where(LongTermMemoryRevision.memory_id == memory_id).order_by(LongTermMemoryRevision.version))).scalars().all())
    assert current_record is None
    assert [revision.version for revision in revisions] == [1, 2]
    assert backend.collections["memory-user-active"] == {}
    assert backend.deleted_items


@pytest.mark.asyncio
async def test_memory_job_control_lifecycle_lists_details_cancels_and_does_not_publish_cancelled_create(
    memory_lifecycle_runtime: tuple[
        async_sessionmaker[AsyncSession],
        FastAPI,
        MemoryJobConsumer,
        MemoryVectorBackend,
        SimpleNamespace,
    ],
) -> None:
    session_factory, app, consumer, backend, _current_user = memory_lifecycle_runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        submitted = await client.post(
            "/api/v1/memories/create",
            json={
                "dedupe_key": "cancelled-create-lifecycle",
                "content": "This record must never be published.",
                "memory_key": "cancelled-create",
                "memory_type": "fact",
            },
        )
        assert submitted.status_code == 200
        job_id = submitted.json()["data"]["job"]["id"]

        jobs = await client.get(
            "/api/v1/memories/jobs",
            params={"status": "pending", "operation": "create"},
        )
        assert jobs.status_code == 200
        assert [item["id"] for item in jobs.json()["data"]["items"]] == [job_id]
        detail = await client.get(f"/api/v1/memories/jobs/{job_id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["id"] == job_id
        assert detail.json()["data"]["status"] == "pending"

        cancelled = await client.post(f"/api/v1/memories/jobs/{job_id}/cancel")
        assert cancelled.status_code == 200
        cancelled_data = cancelled.json()["data"]
        assert cancelled_data["accepted"] is True
        assert cancelled_data["changed"] is True
        assert cancelled_data["job"]["status"] == "cancelled"
        cancelled_again = await client.post(f"/api/v1/memories/jobs/{job_id}/cancel")
        assert cancelled_again.status_code == 200
        cancelled_again_data = cancelled_again.json()["data"]
        assert cancelled_again_data["accepted"] is False
        assert cancelled_again_data["changed"] is False
        assert cancelled_again_data["job"]["status"] == "cancelled"

        assert await consumer.run_once() == 0
        after_cancel = await client.get(f"/api/v1/memories/jobs/{job_id}")
        assert after_cancel.status_code == 200
        assert after_cancel.json()["data"]["status"] == "cancelled"
        hidden = await client.get(
            "/api/v1/memories/list",
            params={"keyword": "cancelled-create"},
        )
        assert hidden.status_code == 200
        assert hidden.json()["data"]["total"] == 0

    async with session_factory() as db:
        records = list(
            (
                await db.execute(
                    select(LongTermMemoryRecord).where(
                        LongTermMemoryRecord.uid == "memory-user",
                        LongTermMemoryRecord.memory_key == "cancelled-create",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert records == []
    assert backend.collections == {}


@pytest.mark.asyncio
async def test_failed_delete_cleanup_lifecycle_retries_through_api_and_worker_until_record_is_physically_removed(
    memory_lifecycle_runtime: tuple[
        async_sessionmaker[AsyncSession],
        FastAPI,
        MemoryJobConsumer,
        MemoryVectorBackend,
        SimpleNamespace,
    ],
) -> None:
    session_factory, app, consumer, backend, _current_user = memory_lifecycle_runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/memories/create",
            json={
                "dedupe_key": "delete-retry-create",
                "content": "Delete retry lifecycle content.",
                "memory_key": "delete-retry",
                "memory_type": "fact",
            },
        )
        assert created.status_code == 200
        create_job_id = created.json()["data"]["job"]["id"]
        create_job = await _run_job_to_terminal(
            session_factory,
            consumer,
            job_id=create_job_id,
        )
        assert create_job.status == LongTermMemoryMutationStatus.SUCCEEDED
        assert create_job.memory_id is not None
        memory_id = create_job.memory_id

        backend.delete_error = RuntimeError("temporary vector delete failure")
        deleted = await client.post(
            "/api/v1/memories/delete",
            json={
                "memory_id": memory_id,
                "expected_version": 1,
                "dedupe_key": "delete-retry-failure",
                "max_attempts": 1,
            },
        )
        assert deleted.status_code == 200
        failed_job_id = deleted.json()["data"]["job"]["id"]
        failed_job = await _run_job_to_terminal(
            session_factory,
            consumer,
            job_id=failed_job_id,
        )
        assert failed_job.status == LongTermMemoryMutationStatus.FAILED
        assert failed_job.error

        failed_detail = await client.get(f"/api/v1/memories/jobs/{failed_job_id}")
        assert failed_detail.status_code == 200
        assert failed_detail.json()["data"]["status"] == "failed"
        failed_jobs = await client.get(
            "/api/v1/memories/jobs",
            params={"status": "failed", "operation": "delete_cleanup"},
        )
        assert failed_jobs.status_code == 200
        assert [item["id"] for item in failed_jobs.json()["data"]["items"]] == [failed_job_id]

        retried = await client.post(f"/api/v1/memories/jobs/{failed_job_id}/retry")
        assert retried.status_code == 200
        retry_data = retried.json()["data"]
        assert retry_data["status"] == "accepted"
        retry_job_id = retry_data["job"]["id"]
        assert retry_job_id != failed_job_id
        assert retry_data["job"]["operation"] == "delete_cleanup"
        cannot_cancel_cleanup = await client.post(f"/api/v1/memories/jobs/{retry_job_id}/cancel")
        assert cannot_cancel_cleanup.status_code == 200
        cleanup_cancel_data = cannot_cancel_cleanup.json()["data"]
        assert cleanup_cancel_data["accepted"] is False
        assert cleanup_cancel_data["changed"] is False
        assert cleanup_cancel_data["job"]["status"] == "pending"

        backend.delete_error = None
        retry_job = await _run_job_to_terminal(
            session_factory,
            consumer,
            job_id=retry_job_id,
        )
        assert retry_job.status == LongTermMemoryMutationStatus.SUCCEEDED
        missing = await client.get(
            "/api/v1/memories/get",
            params={"memory_id": memory_id},
        )
        assert missing.status_code == 404
        history = await client.get(f"/api/v1/memories/{memory_id}/history")
        assert history.status_code == 200
        assert [item["version"] for item in history.json()["data"]["items"]] == [1]

    async with session_factory() as db:
        assert await db.get(LongTermMemoryRecord, memory_id) is None
    assert backend.collections["memory-user-active"] == {}
    assert len(backend.deleted_items) >= 2
