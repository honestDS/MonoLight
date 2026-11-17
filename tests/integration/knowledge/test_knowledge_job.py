from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.constants import (
    ERR_KB_CONTENT_ENCODING_ERROR,
    ERR_KB_CONTENT_TOO_LONG,
    KNOWLEDGE_CONTENT_MAX_CHARS,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.managed import managed_knowledge_item_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.exceptions import BaseBusinessException, ParameterException
from app.core.knowledge.managed import managed_knowledge_service
from app.core.knowledge.organization import create_knowledge_organization_snapshot
from app.core.knowledge.recall import (
    filter_recallable_managed_hits,
    materialize_recallable_managed_hits,
)
from app.core.knowledge.results import ManagedKnowledgeMutationStatus
from app.core.knowledge_jobs import collection_cleanup as collection_cleanup_service
from app.core.knowledge_jobs.executor import (
    KnowledgeJobExecutionError,
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
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationSnapshotItem,
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
    KnowledgeBaseCollectionOwner.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeOrganizationSnapshot.__table__,
    KnowledgeOrganizationSnapshotItem.__table__,
    KnowledgeJob.__table__,
)


@pytest_asyncio.fixture
async def knowledge_job_database(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(tables=_TABLES, foreign_keys=True) as session_factory:
        yield session_factory


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


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        ("x" * (KNOWLEDGE_CONTENT_MAX_CHARS + 1), ERR_KB_CONTENT_TOO_LONG),
        ("invalid\ud800", ERR_KB_CONTENT_ENCODING_ERROR),
    ],
    ids=["too-many-characters", "invalid-encoding"],
)
@pytest.mark.asyncio
async def test_managed_create_submission_rejects_invalid_content_without_persistence(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    content: str,
    expected_message: str,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)

    with pytest.raises(ParameterException) as exc_info:
        async with knowledge_job_database() as db:
            await knowledge_job_manager.submit_create(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base.id,
                knowledge_key="project.architecture",
                content=content,
                source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                actor=ManagedKnowledgeActorType.LLM,
                dedupe_key="invalid-content-create",
            )

    assert exc_info.value.code == 400
    assert exc_info.value.message == expected_message

    async with knowledge_job_database() as db:
        job_count = await db.scalar(select(func.count()).select_from(KnowledgeJob))
        item_count = await db.scalar(select(func.count()).select_from(ManagedKnowledgeItem))
        revision_count = await db.scalar(select(func.count()).select_from(ManagedKnowledgeRevision))

    assert job_count == 0
    assert item_count == 0
    assert revision_count == 0


@pytest.mark.parametrize(
    ("content", "expected_message"),
    [
        ("x" * (KNOWLEDGE_CONTENT_MAX_CHARS + 1), ERR_KB_CONTENT_TOO_LONG),
        ("invalid\ud800", ERR_KB_CONTENT_ENCODING_ERROR),
    ],
    ids=["too-many-characters", "invalid-encoding"],
)
@pytest.mark.asyncio
async def test_managed_update_submission_rejects_invalid_content_without_mutation(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    content: str,
    expected_message: str,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    original_content = "Original content"
    item = await _create_unpublished_item(
        knowledge_job_database,
        knowledge_base.id,
        knowledge_key="update.rejected",
        content=original_content,
    )

    with pytest.raises(ParameterException) as exc_info:
        async with knowledge_job_database() as db:
            await knowledge_job_manager.submit_update(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base.id,
                knowledge_id=item.id,
                expected_version=1,
                knowledge_key=item.knowledge_key,
                content=content,
                source_type=ManagedKnowledgeSourceType.USER_API,
                actor=ManagedKnowledgeActorType.USER,
                dedupe_key="invalid-content-update",
            )

    assert exc_info.value.code == 400
    assert exc_info.value.message == expected_message

    async with knowledge_job_database() as db:
        persisted_item = await db.get(ManagedKnowledgeItem, item.id)
        job_count = await db.scalar(select(func.count()).select_from(KnowledgeJob))
        revision_count = await db.scalar(select(func.count()).select_from(ManagedKnowledgeRevision))

    assert persisted_item is not None
    assert persisted_item.content == original_content
    assert persisted_item.version == 1
    assert job_count == 0
    assert revision_count == 1


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
        RuntimeError("UNIQUE constraint failed: knowledge_job.uid, knowledge_job.active_change_key"),
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
async def test_managed_publication_runs_external_calls_without_database_session(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    job_id = submission.job.id
    async with knowledge_job_database() as db:
        revision_before_publication = (
            await db.execute(
                select(ManagedKnowledgeRevision).where(
                    ManagedKnowledgeRevision.knowledge_id == submission.item.id,
                    ManagedKnowledgeRevision.version == 1,
                )
            )
        ).scalar_one()
        immutable_revision_snapshot = dict(revision_before_publication.after_snapshot)
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
        revision = (
            await db.execute(
                select(ManagedKnowledgeRevision).where(
                    ManagedKnowledgeRevision.knowledge_id == submission.item.id,
                    ManagedKnowledgeRevision.version == 1,
                )
            )
        ).scalar_one()
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base.id)
        job = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=job_id)
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
        )
    assert item is not None
    assert item.indexed_version == item.version == 1
    assert item.is_recallable is True
    assert item.pending_job_id is None
    assert len(item.vector_item_ids) == 1
    assert revision.after_snapshot == immutable_revision_snapshot
    assert snapshot.item_count == 1
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
        await db.execute(update(KnowledgeJob).where(KnowledgeJob.id == cleanup.id).values(available_at=now))
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


@pytest.mark.asyncio
async def test_managed_chunks_deduplicate_before_rerank_and_materialize_after_selection(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    item_id = submission.item.id
    full_content = submission.item.content
    vector_ids = [
        f"managed_{knowledge_base.id}_{item_id}_v1_chunk_0",
        f"managed_{knowledge_base.id}_{item_id}_v1_chunk_1",
    ]

    async with knowledge_job_database() as db:
        await db.execute(
            update(ManagedKnowledgeItem)
            .where(ManagedKnowledgeItem.id == item_id)
            .values(
                indexed_version=1,
                vector_item_ids=vector_ids,
                is_recallable=True,
                pending_job_id=None,
                source_reference={"title": "Architecture note", "session_id": "session-1"},
            )
        )
        await db.commit()

        hits = [
            RetrievalHit(
                id=vector_ids[0],
                content="partial chunk zero",
                metadata={
                    "knowledge_type": "managed",
                    "managed_knowledge_id": item_id,
                    "managed_knowledge_version": 1,
                    "chunk_index": 0,
                },
                fusion_score=0.9,
                rerank_score=0.8,
                rerank_rank=1,
            ),
            RetrievalHit(
                id=vector_ids[1],
                content="partial chunk one",
                metadata={
                    "knowledge_type": "managed",
                    "managed_knowledge_id": item_id,
                    "managed_knowledge_version": 1,
                    "chunk_index": 1,
                },
                fusion_score=0.7,
                rerank_score=0.6,
                rerank_rank=2,
            ),
            RetrievalHit(
                id="user-document",
                content="normal user document chunk",
                metadata={"filename": "manual.md"},
                fusion_score=0.5,
            ),
        ]

        filtered = await filter_recallable_managed_hits(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            hits=hits,
        )
        materialized = await materialize_recallable_managed_hits(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            hits=filtered,
        )

    assert len(filtered) == 2
    filtered_managed_hit = filtered[0]
    assert filtered_managed_hit.id == vector_ids[0]
    assert filtered_managed_hit.content == "partial chunk zero"
    assert filtered_managed_hit.fusion_score == 0.9
    assert filtered_managed_hit.rerank_score == 0.8
    assert filtered_managed_hit.rerank_rank == 1
    assert filtered[1].id == "user-document"

    assert len(materialized) == 2
    managed_hit = materialized[0]
    assert managed_hit.id == vector_ids[0]
    assert managed_hit.content == full_content
    assert managed_hit.content not in {"partial chunk zero", "partial chunk one"}
    assert managed_hit.fusion_score == 0.9
    assert managed_hit.rerank_score == 0.8
    assert managed_hit.rerank_rank == 1
    assert managed_hit.metadata["knowledge_type"] == "managed"
    assert managed_hit.metadata["managed_knowledge_id"] == item_id
    assert managed_hit.metadata["managed_knowledge_key"] == submission.item.knowledge_key
    assert managed_hit.metadata["managed_knowledge_version"] == 1
    assert managed_hit.metadata["managed_knowledge_llm_maintainable"] == submission.item.llm_maintainable
    assert managed_hit.metadata["managed_knowledge_source_type"] == "llm_tool"
    assert managed_hit.metadata["managed_knowledge_source_reference"] == {
        "title": "Architecture note",
        "session_id": "session-1",
    }
    assert materialized[1].id == "user-document"
    assert materialized[1].content == "normal user document chunk"


@pytest.mark.asyncio
async def test_materialize_drops_managed_hit_changed_after_candidate_filter(
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
        hits = [
            RetrievalHit(
                id=vector_id,
                content="old indexed chunk",
                metadata={
                    "knowledge_type": "managed",
                    "managed_knowledge_id": item_id,
                    "managed_knowledge_version": 1,
                },
            ),
            RetrievalHit(
                id="user-document",
                content="stable user document",
                metadata={"filename": "manual.md"},
            ),
        ]
        filtered = await filter_recallable_managed_hits(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            hits=hits,
        )
        assert [hit.id for hit in filtered] == [vector_id, "user-document"]

        await db.execute(
            update(ManagedKnowledgeItem)
            .where(ManagedKnowledgeItem.id == item_id)
            .values(
                version=2,
                is_recallable=False,
            )
        )
        await db.commit()

        materialized = await materialize_recallable_managed_hits(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            hits=filtered,
        )

    assert [hit.id for hit in materialized] == ["user-document"]


@pytest.mark.asyncio
async def test_materialize_refreshes_managed_state_after_concurrent_update(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    item_id = submission.item.id
    vector_id = f"managed_{knowledge_base.id}_{item_id}_v1_chunk_0"

    async with knowledge_job_database() as first_db:
        await first_db.execute(
            update(ManagedKnowledgeItem)
            .where(ManagedKnowledgeItem.id == item_id)
            .values(
                indexed_version=1,
                vector_item_ids=[vector_id],
                is_recallable=True,
                pending_job_id=None,
            )
        )
        await first_db.commit()

        hit = RetrievalHit(
            id=vector_id,
            content="version one chunk",
            metadata={
                "knowledge_type": "managed",
                "managed_knowledge_id": item_id,
                "managed_knowledge_version": 1,
            },
        )
        filtered = await filter_recallable_managed_hits(
            first_db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            hits=[hit],
        )
        assert len(filtered) == 1
        await first_db.commit()

        async with knowledge_job_database() as second_db:
            await second_db.execute(
                update(ManagedKnowledgeItem)
                .where(ManagedKnowledgeItem.id == item_id)
                .values(
                    version=2,
                    content="version two relation content",
                    indexed_version=1,
                    is_recallable=False,
                    vector_item_ids=[vector_id],
                )
            )
            await second_db.commit()

        materialized = await materialize_recallable_managed_hits(
            first_db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            hits=filtered,
        )

    assert materialized == []


@pytest.mark.asyncio
async def test_failed_managed_publish_can_be_resubmitted_with_new_dedupe_key(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    first = await _submit_create(
        knowledge_job_database,
        knowledge_base.id,
        dedupe_key="publish-failed-first",
    )
    assert first.job is not None
    assert first.item is not None

    claimed = await _claim(
        knowledge_job_database,
        job_id=first.job.id,
        owner="failed-publish-worker",
    )
    assert claimed is not None
    async with knowledge_job_database() as db:
        changed = await knowledge_job_crud.mark_failed(
            db,
            uid="user-1",
            job_id=first.job.id,
            owner="failed-publish-worker",
            error="publication failed",
        )
        assert changed is True
        failed_item = await managed_knowledge_item_crud.get_by_id(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_id=first.item.id,
        )
        assert failed_item is not None
        assert failed_item.pending_job_id is None

    async with knowledge_job_database() as db:
        second = await knowledge_job_manager.retry_failed_publication(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_id=first.item.id,
            expected_version=first.item.version,
            failed_job_id=first.job.id,
            dedupe_key="publish-failed-second",
        )

    assert second.job is not None
    assert second.job.id != first.job.id
    assert second.item is not None
    assert second.item.id == first.item.id
    assert second.item.pending_job_id == second.job.id
    assert second.item.is_recallable is False
    assert second.item.indexed_version < second.item.version

    async with knowledge_job_database() as db:
        jobs = list((await db.scalars(select(KnowledgeJob).where(KnowledgeJob.knowledge_base_id == knowledge_base.id).order_by(KnowledgeJob.id.asc()))).all())
    assert [job.status for job in jobs] == [
        KnowledgeJobStatus.FAILED,
        KnowledgeJobStatus.PENDING,
    ]


@pytest.mark.asyncio
async def test_concurrent_failed_publication_retries_create_only_one_active_job(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    first = await _submit_create(
        knowledge_job_database,
        knowledge_base.id,
        dedupe_key="publish-failed-concurrent-source",
    )
    assert first.job is not None
    assert first.item is not None

    claimed = await _claim(
        knowledge_job_database,
        job_id=first.job.id,
        owner="failed-publish-concurrent-worker",
    )
    assert claimed is not None
    async with knowledge_job_database() as db:
        assert await knowledge_job_crud.mark_failed(
            db,
            uid="user-1",
            job_id=first.job.id,
            owner="failed-publish-concurrent-worker",
            error="publication failed",
        )

    async def retry(dedupe_key: str):
        async with knowledge_job_database() as db:
            return await knowledge_job_manager.retry_failed_publication(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base.id,
                knowledge_id=first.item.id,
                expected_version=first.item.version,
                failed_job_id=first.job.id,
                dedupe_key=dedupe_key,
            )

    results = await asyncio.gather(
        retry("publish-failed-concurrent-a"),
        retry("publish-failed-concurrent-b"),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], KnowledgeJobTargetBusyError)

    async with knowledge_job_database() as db:
        active_jobs = list(
            (
                await db.scalars(
                    select(KnowledgeJob).where(
                        KnowledgeJob.knowledge_base_id == knowledge_base.id,
                        KnowledgeJob.active_change_key.is_not(None),
                    )
                )
            ).all()
        )
        item = await managed_knowledge_item_crud.get_by_id(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_id=first.item.id,
        )
    assert len(active_jobs) == 1
    assert item is not None
    assert item.pending_job_id == active_jobs[0].id


@pytest.mark.asyncio
async def test_deleted_container_requeues_cleanup_after_late_vector_write(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="late-write-worker",
    )
    assert claimed is not None

    upsert_started = asyncio.Event()
    release_upsert = asyncio.Event()
    deleted_collections: list[str] = []

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
    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.async_get_or_create_collection",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.embed_texts_with_config",
        AsyncMock(return_value=[[0.1, 0.2]]),
    )

    async def late_upsert(*_args, **_kwargs):
        upsert_started.set()
        await release_upsert.wait()
        return 1

    async def delete_collection_if_exists(collection_name: str) -> bool:
        deleted_collections.append(collection_name)
        return True

    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.async_upsert_collection_items",
        late_upsert,
    )
    monkeypatch.setattr(
        collection_cleanup_service,
        "async_delete_collection_if_exists",
        delete_collection_if_exists,
    )

    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    worker_task = asyncio.create_task(executor.execute_claimed(claimed, "late-write-worker"))
    await upsert_started.wait()

    async with knowledge_job_database() as db:
        current_kb = await db.get(KnowledgeBase, knowledge_base.id)
        assert current_kb is not None
        await db.delete(current_kb)
        await db.commit()

    async with knowledge_job_database() as db:
        first_cleanup = await collection_cleanup_service.process_pending_collection_cleanups(db)
    assert first_cleanup.pending_count == 1
    assert first_cleanup.succeeded_count == 1
    assert deleted_collections == [knowledge_base.collection_name]

    async with knowledge_job_database() as db:
        assert (
            await db.get(
                KnowledgeBaseCollectionOwner,
                knowledge_base.collection_name,
            )
            is None
        )

    release_upsert.set()
    with pytest.raises(KnowledgeJobLeaseLostError):
        await worker_task

    async with knowledge_job_database() as db:
        requeued = await db.get(
            KnowledgeBaseCollectionOwner,
            knowledge_base.collection_name,
        )
        assert requeued is not None
        assert requeued.knowledge_base_id is None
        assert requeued.cleanup_revision == 1
        second_cleanup = await collection_cleanup_service.process_pending_collection_cleanups(db)

    assert second_cleanup.pending_count == 1
    assert second_cleanup.succeeded_count == 1
    assert deleted_collections == [
        knowledge_base.collection_name,
        knowledge_base.collection_name,
    ]

    async with knowledge_job_database() as db:
        assert (
            await db.get(
                KnowledgeBaseCollectionOwner,
                knowledge_base.collection_name,
            )
            is None
        )
