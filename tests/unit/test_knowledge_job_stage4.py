from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.knowledge_base import knowledge_base_crud
from app.core.crud.knowledge_job import knowledge_job_crud
from app.core.crud.managed_knowledge import managed_knowledge_item_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.exceptions import BaseBusinessException
from app.core.knowledge.managed import managed_knowledge_service
from app.core.knowledge.recall import filter_recallable_managed_hits
from app.core.knowledge.results import ManagedKnowledgeMutationStatus
from app.core.knowledge_jobs.consumer import KnowledgeJobConsumer
from app.core.knowledge_jobs.executor import (
    KnowledgeJobCancelledError,
    KnowledgeJobExecutionError,
    KnowledgeJobExecutor,
    KnowledgeJobLeaseLostError,
    KnowledgeJobRetryableError,
)
from app.core.knowledge_jobs.handlers import create_default_knowledge_job_executor
from app.core.knowledge_jobs.manager import (
    KnowledgeJobError,
    KnowledgeJobTargetBusyError,
    knowledge_job_manager,
)
from app.core.knowledge_jobs.vector_cleanup import create_managed_vector_cleanup_job
from app.core.retrieval.schemas import RetrievalHit
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseIndexStatus,
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
from app.providers.database.time import get_database_time

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeJob.__table__,
)


@pytest_asyncio.fixture
async def knowledge_job_database(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "knowledge-job-stage4.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=_TABLES)
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _create_container(session_factory: async_sessionmaker[AsyncSession]) -> KnowledgeBase:
    async with session_factory() as db:
        channel = ModelChannel(
            name="knowledge-job-embedding",
            api_key="enc:v1:test-api-key",
            base_url="https://embedding.invalid/v1",
            model_ids=[],
        )
        db.add(channel)
        await db.flush()

        library = PromptLibrary(name="knowledge-job-prompts", uid="user-1", content="prompt")
        db.add(library)
        await db.flush()

        profile = Profile(name="knowledge-job-profile", uid="user-1", prompt_id=library.id, configs={})
        db.add(profile)
        await db.flush()

        knowledge_base = KnowledgeBase(
            uid="user-1",
            name="managed",
            embedding_channel_id=channel.id,
            embedding_model_id="embedding-model",
            embedding_dimensions=2,
            collection_name="managed-collection",
            knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
            managed_profile_id=profile.id,
            active_embedding_channel_id=channel.id,
            active_embedding_model_id="embedding-model",
            active_embedding_dimensions=2,
            active_embedding_revision=1,
            active_collection_name="managed-collection",
            index_revision=1,
        )
        db.add(knowledge_base)
        await db.commit()
        await db.refresh(knowledge_base)
        return knowledge_base


async def _submit_create(
    session_factory: async_sessionmaker[AsyncSession],
    knowledge_base_id: int,
    *,
    dedupe_key: str = "create:architecture",
):
    async with session_factory() as db:
        return await knowledge_job_manager.submit_create(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
            knowledge_key="project.architecture",
            content="The project uses an event driven architecture.",
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
            dedupe_key=dedupe_key,
        )


async def _create_unpublished_item(
    session_factory: async_sessionmaker[AsyncSession],
    knowledge_base_id: int,
    *,
    knowledge_key: str,
    content: str,
) -> ManagedKnowledgeItem:
    async with session_factory() as db:
        result = await managed_knowledge_service.create(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
            knowledge_key=knowledge_key,
            content=content,
            source_type=ManagedKnowledgeSourceType.USER_API,
            actor=ManagedKnowledgeActorType.USER,
        )
    assert result.item is not None
    return result.item


async def _claim(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: int,
    owner: str,
    lease_seconds: int = 30,
) -> KnowledgeJob | None:
    async with session_factory() as db:
        return await knowledge_job_crud.try_claim(
            db,
            uid="user-1",
            job_id=job_id,
            owner=owner,
            lease_seconds=lease_seconds,
        )


def test_knowledge_job_business_errors_follow_project_exception_contract() -> None:
    assert issubclass(KnowledgeJobError, BaseBusinessException)
    assert issubclass(KnowledgeJobExecutionError, BaseBusinessException)


@pytest.mark.asyncio
async def test_managed_create_submission_is_atomic_and_idempotent(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)

    first = await _submit_create(knowledge_job_database, knowledge_base.id)
    second = await _submit_create(knowledge_job_database, knowledge_base.id)

    assert first.created is True
    assert first.status == ManagedKnowledgeMutationStatus.CREATED
    assert first.job is not None and first.job.id is not None
    assert first.item is not None and first.item.id is not None
    assert first.item.pending_job_id == first.job.id
    assert first.item.source_job_id == first.job.id
    assert first.item.is_recallable is False
    assert second.created is False
    assert second.job is not None and second.job.id == first.job.id
    assert second.item is not None and second.item.id == first.item.id

    async with knowledge_job_database() as db:
        job_count = await db.scalar(select(func.count()).select_from(KnowledgeJob))
        item_count = await db.scalar(select(func.count()).select_from(ManagedKnowledgeItem))
        revision_count = await db.scalar(select(func.count()).select_from(ManagedKnowledgeRevision))
    assert job_count == 1
    assert item_count == 1
    assert revision_count == 1


@pytest.mark.asyncio
async def test_duplicate_content_create_does_not_rebind_job_to_other_item(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    existing = await _create_unpublished_item(
        knowledge_job_database,
        knowledge_base.id,
        knowledge_key="existing.key",
        content="Same exact content",
    )

    async with knowledge_job_database() as db:
        result = await knowledge_job_manager.submit_create(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_key="different.key",
            content="Same exact content",
            source_type=ManagedKnowledgeSourceType.USER_API,
            actor=ManagedKnowledgeActorType.USER,
            dedupe_key="duplicate-content-create",
        )
        item_count = await db.scalar(select(func.count()).select_from(ManagedKnowledgeItem))
        job_count = await db.scalar(select(func.count()).select_from(KnowledgeJob))

    assert result.status == ManagedKnowledgeMutationStatus.EXISTING_CONTENT
    assert result.item is not None and result.item.id == existing.id
    assert result.job is None
    assert item_count == 1
    assert job_count == 0


@pytest.mark.asyncio
async def test_duplicate_content_update_keeps_original_target_unchanged(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    item_a = await _create_unpublished_item(
        knowledge_job_database,
        knowledge_base.id,
        knowledge_key="item.a",
        content="Content A",
    )
    item_b = await _create_unpublished_item(
        knowledge_job_database,
        knowledge_base.id,
        knowledge_key="item.b",
        content="Content B",
    )

    async with knowledge_job_database() as db:
        result = await knowledge_job_manager.submit_update(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_id=item_a.id,
            expected_version=item_a.version,
            knowledge_key="item.a",
            content="Content B",
            source_type=ManagedKnowledgeSourceType.USER_API,
            actor=ManagedKnowledgeActorType.USER,
            dedupe_key="duplicate-content-update",
        )
        current_a = await managed_knowledge_item_crud.get_by_id(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_id=item_a.id,
        )
        job_count = await db.scalar(select(func.count()).select_from(KnowledgeJob))

    assert result.status == ManagedKnowledgeMutationStatus.EXISTING_CONTENT
    assert result.item is not None and result.item.id == item_b.id
    assert result.job is None
    assert current_a is not None and current_a.content == "Content A"
    assert job_count == 0


@pytest.mark.asyncio
async def test_normalized_knowledge_key_uses_single_active_target(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    async with knowledge_job_database() as db:
        first = await knowledge_job_manager.submit_create(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_key="normalized   key",
            content="normalized key content",
            source_type=ManagedKnowledgeSourceType.USER_API,
            actor=ManagedKnowledgeActorType.USER,
            dedupe_key="normalized-key-first",
        )
    assert first.job is not None

    async with knowledge_job_database() as db:
        with pytest.raises(KnowledgeJobTargetBusyError):
            await knowledge_job_manager.submit_create(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base.id,
                knowledge_key="normalized key",
                content="different content",
                source_type=ManagedKnowledgeSourceType.USER_API,
                actor=ManagedKnowledgeActorType.USER,
                dedupe_key="normalized-key-second",
            )


@pytest.mark.asyncio
async def test_active_change_unique_conflict_is_classified_without_state_requery(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    integrity_error = IntegrityError(
        "INSERT INTO knowledge_job",
        {},
        RuntimeError(
            "UNIQUE constraint failed: knowledge_job.uid, knowledge_job.active_change_key"
        ),
    )
    monkeypatch.setattr(
        knowledge_job_crud,
        "create",
        AsyncMock(side_effect=integrity_error),
    )
    active_lookup = AsyncMock(side_effect=AssertionError("must not re-query active state"))
    monkeypatch.setattr(knowledge_job_crud, "get_by_active_change_key", active_lookup)

    async with knowledge_job_database() as db:
        with pytest.raises(KnowledgeJobTargetBusyError):
            await knowledge_job_manager.submit_create(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base.id,
                knowledge_key="busy.key",
                content="busy content",
                source_type=ManagedKnowledgeSourceType.USER_API,
                actor=ManagedKnowledgeActorType.USER,
                dedupe_key="busy-conflict",
            )

    active_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_lease_recovers_and_old_worker_is_fenced(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    job_id = submission.job.id

    first_claim = await _claim(
        knowledge_job_database,
        job_id=job_id,
        owner="old-worker",
        lease_seconds=1,
    )
    assert first_claim is not None

    async with knowledge_job_database() as db:
        now = await get_database_time(db)
        await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == job_id)
            .values(lock_until=now - timedelta(seconds=10))
        )
        await db.commit()
        recovery = await knowledge_job_crud.recover_expired(db, delay_seconds=0)
    assert recovery.retried == 1

    second_claim = await _claim(knowledge_job_database, job_id=job_id, owner="new-worker")
    assert second_claim is not None
    assert second_claim.attempt_count == 2

    async with knowledge_job_database() as db:
        assert (
            await knowledge_job_crud.get_active_claim(
                db,
                uid="user-1",
                job_id=job_id,
                owner="old-worker",
            )
            is None
        )
        assert not await knowledge_job_crud.mark_succeeded(
            db,
            uid="user-1",
            job_id=job_id,
            owner="old-worker",
            result={"stale": True},
        )


@pytest.mark.asyncio
async def test_managed_publication_runs_external_calls_without_database_session(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    job_id = submission.job.id
    claimed = await _claim(knowledge_job_database, job_id=job_id, owner="worker-1")
    assert claimed is not None

    class TrackingFactory:
        def __init__(self) -> None:
            self.active_sessions = 0

        def __call__(self):
            @asynccontextmanager
            async def _session_context():
                async with knowledge_job_database() as db:
                    self.active_sessions += 1
                    try:
                        yield db
                    finally:
                        self.active_sessions -= 1

            return _session_context()

    tracking_factory = TrackingFactory()

    async def fake_load_embedding_runtime_config(_db, channel_id, model_id):
        assert tracking_factory.active_sessions == 1
        return EmbeddingRuntimeConfig(
            channel_id=channel_id,
            channel_name="test",
            model_id=model_id,
            declared_dimensions=2,
            protocol="openai_embedding",
            timeout=30.0,
            base_url="https://embedding.invalid/v1",
            api_key="secret",
        )

    async def fake_get_or_create_collection(*_args, **_kwargs):
        assert tracking_factory.active_sessions == 0

    async def fake_embed(_config, texts, **_kwargs):
        assert tracking_factory.active_sessions == 0
        return [[0.1, 0.2] for _ in texts]

    async def fake_upsert(*_args, **_kwargs):
        assert tracking_factory.active_sessions == 0
        return 1

    monkeypatch.setattr("app.core.knowledge_jobs.handlers.load_embedding_runtime_config", fake_load_embedding_runtime_config)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", fake_get_or_create_collection)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", fake_embed)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", fake_upsert)

    executor = create_default_knowledge_job_executor(session_factory=tracking_factory)
    result = await executor.execute_claimed(claimed, "worker-1")
    assert result.finalized is True
    assert tracking_factory.active_sessions == 0

    async with knowledge_job_database() as db:
        item = await db.get(ManagedKnowledgeItem, submission.item.id)
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base.id)
        job = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=job_id)
    assert item is not None
    assert item.indexed_version == item.version == 1
    assert item.is_recallable is True
    assert item.pending_job_id is None
    assert len(item.vector_item_ids) == 1
    assert current_knowledge_base is not None
    assert current_knowledge_base.index_status == KnowledgeBaseIndexStatus.READY
    assert job is not None and job.status == KnowledgeJobStatus.SUCCEEDED
    assert job.active_change_key is None
    assert job.locked_by is None


@pytest.mark.asyncio
async def test_managed_initial_ready_does_not_override_reindexing(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    async with knowledge_job_database() as db:
        current = await db.get(KnowledgeBase, knowledge_base.id)
        assert current is not None
        current.index_status = KnowledgeBaseIndexStatus.REINDEXING
        db.add(current)
        await db.commit()

    async with knowledge_job_database() as db:
        valid_target = await knowledge_base_crud.mark_managed_initial_index_ready(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            active_collection_name=knowledge_base.active_collection_name,
            commit=True,
        )

    assert valid_target is True
    async with knowledge_job_database() as db:
        current = await db.get(KnowledgeBase, knowledge_base.id)
    assert current is not None
    assert current.index_status == KnowledgeBaseIndexStatus.REINDEXING


@pytest.mark.asyncio
async def test_delete_tombstones_immediately_and_cleanup_failure_remains_retryable(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    job_id = submission.job.id
    claimed = await _claim(knowledge_job_database, job_id=job_id, owner="publish-worker")
    assert claimed is not None

    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.load_embedding_runtime_config",
        AsyncMock(
            return_value=EmbeddingRuntimeConfig(
                channel_id=1,
                channel_name="test",
                model_id="embedding-model",
                declared_dimensions=2,
                protocol="openai_embedding",
                timeout=30.0,
                base_url="https://embedding.invalid/v1",
                api_key="secret",
            )
        ),
    )
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", AsyncMock())
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", AsyncMock(return_value=[[0.1, 0.2]]))
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", AsyncMock(return_value=1))
    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    await executor.execute_claimed(claimed, "publish-worker")

    async with knowledge_job_database() as db:
        current = await db.get(ManagedKnowledgeItem, submission.item.id)
        delete_submission = await knowledge_job_manager.submit_delete(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_id=current.id,
            expected_version=current.version,
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
            dedupe_key="delete:architecture",
        )

    assert delete_submission.status == ManagedKnowledgeMutationStatus.DELETED
    assert delete_submission.item.is_recallable is False
    assert delete_submission.item.deleted_at is not None
    assert delete_submission.item.pending_job_id == delete_submission.job.id

    delete_claim = await _claim(
        knowledge_job_database,
        job_id=delete_submission.job.id,
        owner="delete-worker",
    )
    assert delete_claim is not None

    async def fail_delete(*_args, **_kwargs):
        raise RuntimeError("vector unavailable")

    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_delete_collection_items", fail_delete)
    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    with pytest.raises(KnowledgeJobRetryableError):
        await executor.execute_claimed(delete_claim, "delete-worker")

    async with knowledge_job_database() as db:
        assert await knowledge_job_crud.release_for_retry(
            db,
            uid="user-1",
            job_id=delete_submission.job.id,
            owner="delete-worker",
            error="retryable",
            delay_seconds=0,
        )
        current = await db.get(ManagedKnowledgeItem, submission.item.id)
        job = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=delete_submission.job.id)
    assert current is not None
    assert current.deleted_at is not None
    assert current.is_recallable is False
    assert job is not None and job.status == KnowledgeJobStatus.RETRY


@pytest.mark.asyncio
async def test_vector_cleanup_job_failure_is_independently_retryable(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    parent_claim = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="publish-worker",
    )
    assert parent_claim is not None

    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.load_embedding_runtime_config",
        AsyncMock(
            return_value=EmbeddingRuntimeConfig(
                channel_id=1,
                channel_name="test",
                model_id="embedding-model",
                declared_dimensions=2,
                protocol="openai_embedding",
                timeout=30.0,
                base_url="https://embedding.invalid/v1",
                api_key="secret",
            )
        ),
    )
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", AsyncMock())
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", AsyncMock(return_value=[[0.1, 0.2]]))
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", AsyncMock(return_value=1))
    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    await executor.execute_claimed(parent_claim, "publish-worker")

    async with knowledge_job_database() as db:
        parent = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert parent is not None and parent.status == KnowledgeJobStatus.SUCCEEDED
        cleanup = await create_managed_vector_cleanup_job(
            db,
            source_job=parent,
            reason="superseded",
            collection_name=knowledge_base.collection_name,
            vector_item_ids=["orphan-vector"],
        )
        now = await get_database_time(db)
        await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == cleanup.id)
            .values(available_at=now)
        )
        await db.commit()

    cleanup_claim = await _claim(
        knowledge_job_database,
        job_id=cleanup.id,
        owner="cleanup-worker",
    )
    assert cleanup_claim is not None

    async def fail_delete(*_args, **_kwargs):
        raise RuntimeError("vector unavailable")

    monkeypatch.setattr(
        "app.core.knowledge_jobs.vector_cleanup.async_delete_collection_items",
        fail_delete,
    )
    with pytest.raises(KnowledgeJobRetryableError):
        await executor.execute_claimed(cleanup_claim, "cleanup-worker")

    async with knowledge_job_database() as db:
        assert await knowledge_job_crud.release_for_retry(
            db,
            uid="user-1",
            job_id=cleanup.id,
            owner="cleanup-worker",
            error="retryable",
            delay_seconds=0,
        )
        current = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=cleanup.id,
        )
    assert current is not None and current.status == KnowledgeJobStatus.RETRY


@pytest.mark.asyncio
async def test_vector_cleanup_job_is_not_claimable_before_parent_is_terminal(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    parent_claim = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="publish-worker",
    )
    assert parent_claim is not None

    async with knowledge_job_database() as db:
        cleanup = await create_managed_vector_cleanup_job(
            db,
            source_job=parent_claim,
            reason="staged",
            collection_name=knowledge_base.collection_name,
            vector_item_ids=["staged-vector"],
        )
        await db.commit()

    assert (
        await _claim(
            knowledge_job_database,
            job_id=cleanup.id,
            owner="cleanup-worker",
        )
        is None
    )

    async with knowledge_job_database() as db:
        assert await knowledge_job_crud.mark_failed(
            db,
            uid="user-1",
            job_id=submission.job.id,
            owner="publish-worker",
            error="failed",
        )

    assert (
        await _claim(
            knowledge_job_database,
            job_id=cleanup.id,
            owner="cleanup-worker",
        )
        is not None
    )


@pytest.mark.asyncio
async def test_system_vector_cleanup_job_cannot_be_cancelled(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    parent_claim = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="publish-worker",
    )
    assert parent_claim is not None

    async with knowledge_job_database() as db:
        cleanup = await create_managed_vector_cleanup_job(
            db,
            source_job=parent_claim,
            reason="staged",
            collection_name=knowledge_base.collection_name,
            vector_item_ids=["staged-vector"],
        )
        await db.commit()
        cancellation = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=cleanup.id,
        )
        current = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=cleanup.id,
        )

    assert cancellation.accepted is False
    assert cancellation.changed is False
    assert current is not None and current.status == KnowledgeJobStatus.PENDING
    assert current.cancel_requested_at is None


@pytest.mark.asyncio
async def test_consumer_startup_recovers_expired_cleanup_even_after_max_attempts(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    parent_claim = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="publish-worker",
    )
    assert parent_claim is not None

    async with knowledge_job_database() as db:
        assert await knowledge_job_crud.mark_failed(
            db,
            uid="user-1",
            job_id=submission.job.id,
            owner="publish-worker",
            error="parent failed",
        )
        parent = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert parent is not None
        cleanup = await create_managed_vector_cleanup_job(
            db,
            source_job=parent,
            reason="staged",
            collection_name=knowledge_base.collection_name,
            vector_item_ids=["orphan-after-restart"],
        )
        await db.commit()

    cleanup_claim = await _claim(
        knowledge_job_database,
        job_id=cleanup.id,
        owner="crashed-cleanup-worker",
        lease_seconds=1,
    )
    assert cleanup_claim is not None

    async with knowledge_job_database() as db:
        now = await get_database_time(db)
        await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == cleanup.id)
            .values(
                attempt_count=cleanup_claim.max_attempts,
                lock_until=now - timedelta(seconds=10),
            )
        )
        await db.commit()

    startup_recovered = asyncio.Event()

    class _StartupRecoveryConsumer(KnowledgeJobConsumer):
        async def _recover_expired(self):
            result = await super()._recover_expired()
            startup_recovered.set()
            return result

    consumer = _StartupRecoveryConsumer(
        KnowledgeJobExecutor({}),
        session_factory=knowledge_job_database,
        poll_interval_seconds=60,
        recovery_interval_seconds=60,
    )
    consumer.start()
    await asyncio.wait_for(startup_recovered.wait(), timeout=1)
    await consumer.stop()

    async with knowledge_job_database() as db:
        current = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=cleanup.id,
        )

    assert current is not None
    assert current.status == KnowledgeJobStatus.RETRY
    assert current.locked_by is None
    assert current.lock_until is None
    assert current.attempt_count == current.max_attempts


@pytest.mark.asyncio
async def test_retryable_cleanup_continues_after_normal_max_attempts(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    parent_claim = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="publish-worker",
    )
    assert parent_claim is not None

    async with knowledge_job_database() as db:
        assert await knowledge_job_crud.mark_failed(
            db,
            uid="user-1",
            job_id=submission.job.id,
            owner="publish-worker",
            error="parent failed",
        )
        parent = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert parent is not None
        cleanup = await create_managed_vector_cleanup_job(
            db,
            source_job=parent,
            reason="staged",
            collection_name=knowledge_base.collection_name,
            vector_item_ids=["retry-forever-vector"],
        )
        await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == cleanup.id)
            .values(attempt_count=cleanup.max_attempts - 1)
        )
        await db.commit()

    cleanup_claim = await _claim(
        knowledge_job_database,
        job_id=cleanup.id,
        owner="cleanup-worker",
    )
    assert cleanup_claim is not None
    assert cleanup_claim.attempt_count == cleanup_claim.max_attempts

    async def fail_delete(*_args, **_kwargs):
        raise RuntimeError("vector unavailable")

    monkeypatch.setattr(
        "app.core.knowledge_jobs.vector_cleanup.async_delete_collection_items",
        fail_delete,
    )
    consumer = KnowledgeJobConsumer(
        create_default_knowledge_job_executor(session_factory=knowledge_job_database),
        session_factory=knowledge_job_database,
    )
    await consumer._execute(cleanup_claim, "cleanup-worker")

    async with knowledge_job_database() as db:
        current = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=cleanup.id,
        )
    assert current is not None
    assert current.status == KnowledgeJobStatus.RETRY
    assert current.attempt_count == current.max_attempts


@pytest.mark.asyncio
async def test_lost_lease_checkpoint_rejects_old_worker(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="old-worker",
        lease_seconds=1,
    )
    assert claimed is not None

    async with knowledge_job_database() as db:
        now = await get_database_time(db)
        await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == submission.job.id)
            .values(lock_until=now - timedelta(seconds=10))
        )
        await db.commit()

    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    with pytest.raises(KnowledgeJobLeaseLostError):
        await executor.execute_claimed(claimed, "old-worker")


@pytest.mark.asyncio
async def test_running_cancellation_is_observed_before_lease_expiry(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="cancel-worker",
    )
    assert claimed is not None

    async with knowledge_job_database() as db:
        cancellation = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert cancellation.accepted is True
        assert cancellation.changed is True

    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    with pytest.raises(KnowledgeJobCancelledError):
        await executor.execute_claimed(claimed, "cancel-worker")


@pytest.mark.asyncio
async def test_cancel_request_blocks_terminal_success_race(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="cancel-race-worker",
    )
    assert claimed is not None

    async with knowledge_job_database() as db:
        cancellation = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert cancellation.accepted is True
        assert cancellation.changed is True
        assert not await knowledge_job_crud.mark_succeeded(
            db,
            uid="user-1",
            job_id=submission.job.id,
            owner="cancel-race-worker",
        )
        current = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )

    assert current is not None
    assert current.status == KnowledgeJobStatus.RUNNING
    assert current.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_running_cancel_request_is_idempotent(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="cancel-idempotent-worker",
    )
    assert claimed is not None

    async with knowledge_job_database() as db:
        first = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        second = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )

    assert first.accepted is True and first.changed is True
    assert second.accepted is True and second.changed is False
    assert second.job is not None and second.job.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_cancel_request_retries_after_stale_pending_read(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="cancel-transition-worker",
    )
    assert claimed is not None
    stale = claimed.model_copy(update={"status": KnowledgeJobStatus.PENDING})
    original_get_by_id = knowledge_job_crud.get_by_id
    calls = 0

    async def stale_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return stale
        return await original_get_by_id(*args, **kwargs)

    monkeypatch.setattr(knowledge_job_crud, "get_by_id", stale_once)
    async with knowledge_job_database() as db:
        cancellation = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        current = await original_get_by_id(db, uid="user-1", job_id=submission.job.id)

    assert cancellation.accepted is True
    assert cancellation.changed is True
    assert current is not None
    assert current.status == KnowledgeJobStatus.RUNNING
    assert current.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_shutdown_release_does_not_create_retry_with_cancel_requested(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="shutdown-race-worker",
    )
    assert claimed is not None
    stale = claimed.model_copy(update={"cancel_requested_at": None})

    async with knowledge_job_database() as db:
        cancellation = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert cancellation.accepted is True
        assert cancellation.changed is True

    original_get_by_id = knowledge_job_crud.get_by_id

    async def stale_claim(*_args, **_kwargs):
        return stale

    monkeypatch.setattr(knowledge_job_crud, "get_by_id", stale_claim)
    async with knowledge_job_database() as db:
        changed = await knowledge_job_crud.release_claim_for_shutdown(
            db,
            uid="user-1",
            job_id=submission.job.id,
            owner="shutdown-race-worker",
            delay_seconds=1,
            max_attempts_error="max attempts",
        )

    monkeypatch.setattr(knowledge_job_crud, "get_by_id", original_get_by_id)
    async with knowledge_job_database() as db:
        current = await original_get_by_id(db, uid="user-1", job_id=submission.job.id)

    assert changed is False
    assert current is not None
    assert current.status == KnowledgeJobStatus.RUNNING
    assert current.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_stale_worker_cleanup_cannot_delete_new_worker_published_vectors(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    job_id = submission.job.id
    old_claim = await _claim(knowledge_job_database, job_id=job_id, owner="old-worker")
    assert old_claim is not None

    old_upsert_started = asyncio.Event()
    release_old_upsert = asyncio.Event()
    upsert_call_count = 0
    upserted_ids: list[list[str]] = []
    deleted_ids: list[list[str]] = []

    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.load_embedding_runtime_config",
        AsyncMock(
            return_value=EmbeddingRuntimeConfig(
                channel_id=1,
                channel_name="test",
                model_id="embedding-model",
                declared_dimensions=2,
                protocol="openai_embedding",
                timeout=30.0,
                base_url="https://embedding.invalid/v1",
                api_key="secret",
            )
        ),
    )
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", AsyncMock())

    async def fake_embed(_config, texts, **_kwargs):
        return [[0.1, 0.2] for _ in texts]

    async def fake_upsert(_collection_name, item_ids, *_args, **_kwargs):
        nonlocal upsert_call_count
        upsert_call_count += 1
        upserted_ids.append(list(item_ids))
        if upsert_call_count == 1:
            old_upsert_started.set()
            await release_old_upsert.wait()
        return len(item_ids)

    async def fake_delete(_collection_name, item_ids, **_kwargs):
        deleted_ids.append(list(item_ids))
        return len(item_ids)

    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", fake_embed)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", fake_upsert)
    monkeypatch.setattr("app.core.knowledge_jobs.vector_cleanup.async_delete_collection_items", fake_delete)

    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    old_task = asyncio.create_task(executor.execute_claimed(old_claim, "old-worker"))
    await old_upsert_started.wait()

    async with knowledge_job_database() as db:
        now = await get_database_time(db)
        await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == job_id)
            .values(lock_until=now - timedelta(seconds=10))
        )
        await db.commit()
        recovery = await knowledge_job_crud.recover_expired(db, delay_seconds=0)
    assert recovery.retried == 1

    new_claim = await _claim(knowledge_job_database, job_id=job_id, owner="new-worker")
    assert new_claim is not None
    new_result = await executor.execute_claimed(new_claim, "new-worker")
    assert new_result.finalized is True

    async with knowledge_job_database() as db:
        published_item = await db.get(ManagedKnowledgeItem, submission.item.id)
    assert published_item is not None
    published_ids = set(published_item.vector_item_ids)
    assert published_ids

    release_old_upsert.set()
    with pytest.raises(KnowledgeJobLeaseLostError):
        await old_task

    assert len(upserted_ids) == 2
    old_vector_ids = set(upserted_ids[0])
    async with knowledge_job_database() as db:
        cleanup_result = await db.execute(
            select(KnowledgeJob)
            .where(
                KnowledgeJob.parent_job_id == job_id,
                KnowledgeJob.operation == KnowledgeJobOperation.MANAGED_VECTOR_CLEANUP,
            )
            .order_by(KnowledgeJob.id)
        )
        cleanup_jobs = list(cleanup_result.scalars().all())
        old_cleanup = next(
            job
            for job in cleanup_jobs
            if old_vector_ids == set(job.payload.get("vector_item_ids", []))
        )
        now = await get_database_time(db)
        await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == old_cleanup.id)
            .values(available_at=now)
        )
        await db.commit()

    cleanup_claim = await _claim(
        knowledge_job_database,
        job_id=old_cleanup.id,
        owner="cleanup-worker",
    )
    assert cleanup_claim is not None
    cleanup_result = await executor.execute_claimed(cleanup_claim, "cleanup-worker")
    assert cleanup_result.finalized is False
    assert deleted_ids
    assert published_ids.isdisjoint({item_id for batch in deleted_ids for item_id in batch})


@pytest.mark.asyncio
async def test_unknown_commit_result_does_not_delete_published_vectors(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="commit-race-worker",
    )
    assert claimed is not None

    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.load_embedding_runtime_config",
        AsyncMock(
            return_value=EmbeddingRuntimeConfig(
                channel_id=1,
                channel_name="test",
                model_id="embedding-model",
                declared_dimensions=2,
                protocol="openai_embedding",
                timeout=30.0,
                base_url="https://embedding.invalid/v1",
                api_key="secret",
            )
        ),
    )
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", AsyncMock())
    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.embed_texts_with_config",
        AsyncMock(return_value=[[0.1, 0.2]]),
    )
    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.async_upsert_collection_items",
        AsyncMock(return_value=1),
    )
    delete_mock = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.knowledge_jobs.vector_cleanup.async_delete_collection_items", delete_mock)

    original_commit = AsyncSession.commit
    commit_count = 0

    async def commit_then_raise(session: AsyncSession) -> None:
        nonlocal commit_count
        commit_count += 1
        await original_commit(session)
        if commit_count == 2:
            raise RuntimeError("commit result unknown")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_raise)
    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    with pytest.raises(RuntimeError, match="commit result unknown"):
        await executor.execute_claimed(claimed, "commit-race-worker")

    delete_mock.assert_not_awaited()
    async with knowledge_job_database() as db:
        item = await db.get(ManagedKnowledgeItem, submission.item.id)
        job = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=submission.job.id)
    assert item is not None and item.is_recallable is True
    assert item.vector_item_ids
    assert job is not None and job.status == KnowledgeJobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_tombstoned_managed_vectors_are_filtered_before_cleanup(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    item_id = submission.item.id
    vector_id = f"managed_{knowledge_base.id}_{item_id}_v1_chunk_0"

    async with knowledge_job_database() as db:
        await db.execute(
            update(ManagedKnowledgeItem)
            .where(ManagedKnowledgeItem.id == item_id)
            .values(
                indexed_version=1,
                vector_item_ids=[vector_id],
                is_recallable=True,
                pending_job_id=None,
            )
        )
        await db.commit()
        current = await db.get(ManagedKnowledgeItem, item_id)
        delete_submission = await knowledge_job_manager.submit_delete(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_id=item_id,
            expected_version=current.version,
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
            dedupe_key="delete:filter",
        )
        assert delete_submission.item.deleted_at is not None

        hits = [
            RetrievalHit(
                id=vector_id,
                content="stale managed content",
                metadata={
                    "knowledge_type": "managed",
                    "managed_knowledge_id": item_id,
                    "managed_knowledge_version": 1,
                },
            ),
            RetrievalHit(id="user-document", content="normal content", metadata={}),
        ]
        filtered = await filter_recallable_managed_hits(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            hits=hits,
        )

    assert [hit.id for hit in filtered] == ["user-document"]


@pytest.mark.asyncio
async def test_malformed_managed_vector_metadata_is_not_recalled(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    hits = [
        RetrievalHit(
            id="malformed-managed-vector",
            content="must not be recalled",
            metadata={
                "knowledge_type": "managed",
                "managed_knowledge_id": "invalid",
                "managed_knowledge_version": 1,
            },
        ),
        RetrievalHit(id="user-document", content="normal content", metadata={}),
    ]

    async with knowledge_job_database() as db:
        filtered = await filter_recallable_managed_hits(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            hits=hits,
        )

    assert [hit.id for hit in filtered] == ["user-document"]
