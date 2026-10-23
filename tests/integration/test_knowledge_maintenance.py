from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.crud.knowledge.job import KnowledgeJobRecoveryResult
from app.core.knowledge_jobs import maintenance as maintenance_module
from app.core.knowledge_jobs.consumer import KnowledgeJobConsumer
from app.core.knowledge_jobs.executor import (
    KnowledgeJobExecutionContext,
    KnowledgeJobExecutor,
    KnowledgeJobRetryableError,
)
from app.core.knowledge_jobs.maintenance import (
    handle_knowledge_maintenance,
    submit_startup_knowledge_maintenance_jobs,
)
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseDocument,
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
from app.providers.vector.chroma import CollectionValidationResult

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    KnowledgeBaseCollectionOwner.__table__,
    KnowledgeBaseDocument.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeOrganizationSnapshot.__table__,
    KnowledgeOrganizationSnapshotItem.__table__,
    KnowledgeOrganizationStage.__table__,
    KnowledgeOrganizationFragment.__table__,
    KnowledgeJob.__table__,
)


@pytest_asyncio.fixture
async def session_factory(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        tables=_TABLES,
        foreign_keys=True,
        poolclass=NullPool,
    ) as factory:
        yield factory


async def _create_user_knowledge_base(session_factory: async_sessionmaker[AsyncSession]) -> KnowledgeBase:
    async with session_factory() as db:
        channel = ModelChannel(
            name="knowledge_maintenance-embedding",
            api_key="enc:v1:test-api-key",
            base_url="https://embedding.invalid/v1",
            model_ids=[],
        )
        db.add(channel)
        await db.flush()
        knowledge_base = KnowledgeBase(
            uid="knowledge_maintenance-user",
            name="knowledge_maintenance-user-kb",
            embedding_channel_id=channel.id,
            embedding_model_id="embedding-model",
            embedding_dimensions=2,
            collection_name="knowledge_maintenance-collection",
            knowledge_base_type=KnowledgeBaseType.USER,
            active_embedding_channel_id=channel.id,
            active_embedding_model_id="embedding-model",
            active_embedding_dimensions=2,
            active_embedding_signature="knowledge_maintenance-signature",
            active_embedding_revision=1,
            active_collection_name="knowledge_maintenance-collection",
            index_revision=1,
        )
        db.add(knowledge_base)
        await db.commit()
        await db.refresh(knowledge_base)
        return knowledge_base


async def _claim_maintenance(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    knowledge_base: KnowledgeBase,
    owner: str,
) -> KnowledgeJob:
    async with session_factory() as db:
        await submit_startup_knowledge_maintenance_jobs(db)
        await db.commit()
        job = await db.scalar(
            select(KnowledgeJob).where(
                KnowledgeJob.uid == knowledge_base.uid,
                KnowledgeJob.knowledge_base_id == knowledge_base.id,
                KnowledgeJob.operation == KnowledgeJobOperation.KNOWLEDGE_MAINTENANCE,
            )
        )
        assert job is not None and job.id is not None
        claimed = await maintenance_module.knowledge_job_crud.try_claim(
            db,
            uid=knowledge_base.uid,
            job_id=job.id,
            owner=owner,
            lease_seconds=60,
            enabled_operations=(KnowledgeJobOperation.KNOWLEDGE_MAINTENANCE,),
        )
        assert claimed is not None
        return claimed


@pytest.mark.asyncio
async def test_startup_submission_keeps_only_one_active_maintenance_job_per_knowledge_base(session_factory) -> None:
    knowledge_base = await _create_user_knowledge_base(session_factory)

    async with session_factory() as db:
        first_created = await submit_startup_knowledge_maintenance_jobs(db)
        await db.commit()
        second_created = await submit_startup_knowledge_maintenance_jobs(db)
        await db.commit()
        jobs = list(
            (
                await db.execute(
                    select(KnowledgeJob).where(
                        KnowledgeJob.uid == knowledge_base.uid,
                        KnowledgeJob.knowledge_base_id == knowledge_base.id,
                        KnowledgeJob.operation == KnowledgeJobOperation.KNOWLEDGE_MAINTENANCE,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert first_created == 1
    assert second_created == 0
    assert len(jobs) == 1
    assert jobs[0].status == KnowledgeJobStatus.PENDING
    assert jobs[0].active_change_key == f"kb-maintenance:{knowledge_base.id}"
    assert jobs[0].payload == {
        "knowledge_base_id": knowledge_base.id,
        "trigger": "worker_startup",
    }

    async with session_factory() as db:
        current = await db.get(KnowledgeJob, jobs[0].id)
        assert current is not None
        current.status = KnowledgeJobStatus.SUCCEEDED
        current.active_change_key = None
        await db.commit()
        third_created = await submit_startup_knowledge_maintenance_jobs(db)
        await db.commit()
        all_jobs = list(
            (
                await db.execute(
                    select(KnowledgeJob).where(
                        KnowledgeJob.uid == knowledge_base.uid,
                        KnowledgeJob.knowledge_base_id == knowledge_base.id,
                        KnowledgeJob.operation == KnowledgeJobOperation.KNOWLEDGE_MAINTENANCE,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert third_created == 1
    assert len(all_jobs) == 2
    assert all_jobs[0].dedupe_key != all_jobs[1].dedupe_key


@pytest.mark.asyncio
async def test_consumer_submits_maintenance_only_once_per_worker_start() -> None:
    startup_submissions = 0
    claim_cycles = 0

    async def _maintenance_handler(_context):
        return {}

    class StartupMaintenanceConsumer(KnowledgeJobConsumer):
        async def _recover_expired(self) -> KnowledgeJobRecoveryResult:
            return KnowledgeJobRecoveryResult()

        async def _submit_startup_maintenance_jobs(self) -> bool:
            nonlocal startup_submissions
            startup_submissions += 1
            return True

        async def _process_orphan_collection_cleanups(self) -> None:
            return None

        async def _claim_available(self) -> None:
            nonlocal claim_cycles
            claim_cycles += 1
            if claim_cycles >= 3:
                self._stop_event.set()

    consumer = StartupMaintenanceConsumer(
        KnowledgeJobExecutor({KnowledgeJobOperation.KNOWLEDGE_MAINTENANCE: _maintenance_handler}),
        poll_interval_seconds=0.001,
        recovery_interval_seconds=60,
        collection_cleanup_interval_seconds=60,
    )

    await consumer.start()

    assert startup_submissions == 1
    assert claim_cycles == 3


@pytest.mark.asyncio
async def test_consumer_retries_only_failed_startup_maintenance_submission() -> None:
    startup_submissions = 0
    claim_cycles = 0

    async def _maintenance_handler(_context):
        return {}

    class StartupMaintenanceRetryConsumer(KnowledgeJobConsumer):
        async def _recover_expired(self) -> KnowledgeJobRecoveryResult:
            return KnowledgeJobRecoveryResult()

        async def _submit_startup_maintenance_jobs(self) -> bool:
            nonlocal startup_submissions
            startup_submissions += 1
            return startup_submissions >= 2

        async def _process_orphan_collection_cleanups(self) -> None:
            return None

        async def _claim_available(self) -> None:
            nonlocal claim_cycles
            claim_cycles += 1
            if claim_cycles >= 4:
                self._stop_event.set()

    consumer = StartupMaintenanceRetryConsumer(
        KnowledgeJobExecutor({KnowledgeJobOperation.KNOWLEDGE_MAINTENANCE: _maintenance_handler}),
        poll_interval_seconds=0.001,
        recovery_interval_seconds=60,
        collection_cleanup_interval_seconds=60,
    )

    await consumer.start()

    assert startup_submissions == 2
    assert claim_cycles == 4


@pytest.mark.asyncio
async def test_maintenance_retries_when_another_knowledge_job_is_active(session_factory) -> None:
    knowledge_base = await _create_user_knowledge_base(session_factory)
    async with session_factory() as db:
        db.add(
            KnowledgeJob(
                uid=knowledge_base.uid,
                operation=KnowledgeJobOperation.REINDEX,
                dedupe_key="knowledge_maintenance-active-reindex",
                request_hash="a" * 64,
                active_change_key=f"knowledge_maintenance-reindex:{knowledge_base.id}",
                status=KnowledgeJobStatus.PENDING,
                knowledge_base_id=knowledge_base.id,
                payload={},
            )
        )
        await db.commit()

    claimed = await _claim_maintenance(
        session_factory,
        knowledge_base=knowledge_base,
        owner="knowledge_maintenance-deferred-worker",
    )

    with pytest.raises(KnowledgeJobRetryableError):
        await handle_knowledge_maintenance(
            KnowledgeJobExecutionContext(
                job=claimed,
                worker_id="knowledge_maintenance-deferred-worker",
                session_factory=session_factory,
            )
        )


@pytest.mark.asyncio
async def test_maintenance_cleans_only_orphan_vectors_and_accepts_document_uuid_metadata(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_user_knowledge_base(session_factory)
    async with session_factory() as db:
        document = KnowledgeBaseDocument(
            knowledge_base_id=knowledge_base.id,
            filename="guide.txt",
            content="guide",
            chunk_size=100,
            chunk_overlap=0,
            batch_size=10,
            chunk_count=1,
            chunk_ids=["valid-doc-vector"],
            metadata_={"document_uuid": "doc-uuid"},
        )
        db.add(document)
        await db.commit()

    claimed = await _claim_maintenance(
        session_factory,
        knowledge_base=knowledge_base,
        owner="knowledge_maintenance-maintenance-worker",
    )
    vectors = {
        "valid-doc-vector": {
            "knowledge_base_id": knowledge_base.id,
            "document_uuid": "doc-uuid",
        },
        "orphan-vector": {
            "knowledge_base_id": knowledge_base.id,
            "document_uuid": "missing-doc",
        },
    }
    deleted_ids: list[str] = []

    async def get_items(_collection_name, offset=0, limit=None, include=None):
        del include
        rows = list(vectors.items())
        rows = rows[offset:] if limit is None else rows[offset : offset + limit]
        return {
            "ids": [item_id for item_id, _metadata in rows],
            "metadatas": [metadata for _item_id, metadata in rows],
        }

    async def get_items_by_ids(_collection_name, ids, include=None):
        del include
        return {"ids": [item_id for item_id in ids if item_id in vectors]}

    async def delete_items(_collection_name, ids, batch_size=100):
        del batch_size
        for item_id in ids:
            deleted_ids.append(item_id)
            vectors.pop(item_id, None)
        return len(ids)

    async def validate(
        _collection_name,
        expected_count=None,
        expected_metadata=None,
        expected_dimension=None,
        sample_size=1,
    ) -> CollectionValidationResult:
        del expected_metadata, expected_dimension, sample_size
        errors = () if expected_count is None or len(vectors) == expected_count else ("count_mismatch",)
        return CollectionValidationResult(
            exists=True,
            valid=not errors,
            count=len(vectors),
            errors=errors,
        )

    monkeypatch.setattr(maintenance_module, "async_get_collection_items", get_items)
    monkeypatch.setattr(maintenance_module, "async_get_collection_items_by_ids", get_items_by_ids)
    monkeypatch.setattr(maintenance_module, "async_delete_collection_items", delete_items)
    monkeypatch.setattr(maintenance_module, "async_validate_collection", validate)

    result = await handle_knowledge_maintenance(
        KnowledgeJobExecutionContext(
            job=claimed,
            worker_id="knowledge_maintenance-maintenance-worker",
            session_factory=session_factory,
        )
    )

    assert deleted_ids == ["orphan-vector"]
    assert set(vectors) == {"valid-doc-vector"}
    assert result.result["orphan_vectors_deleted"] == 1
    assert result.result["consistent"] is True
    assert result.result["history_deleted"] == 0
    assert result.result["repair_required"] is False

    async with session_factory() as db:
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base.id)
        migration_jobs = list(
            (
                await db.execute(
                    select(KnowledgeJob).where(
                        KnowledgeJob.knowledge_base_id == knowledge_base.id,
                        KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert current_knowledge_base is not None
    assert current_knowledge_base.index_status == knowledge_base.index_status
    assert migration_jobs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "anomaly",
    ["missing_collection", "missing_vector", "count_mismatch", "dimension_mismatch"],
)
async def test_maintenance_marks_index_failed_without_submitting_embedding_migration(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
    anomaly: str,
) -> None:
    knowledge_base = await _create_user_knowledge_base(session_factory)
    async with session_factory() as db:
        db.add(
            KnowledgeBaseDocument(
                knowledge_base_id=knowledge_base.id,
                filename=f"{anomaly}.txt",
                content=anomaly,
                chunk_size=100,
                chunk_overlap=0,
                batch_size=10,
                chunk_count=1,
                chunk_ids=["expected-vector"],
                metadata_={"document_uuid": "expected-uuid"},
            )
        )
        await db.commit()

    claimed = await _claim_maintenance(
        session_factory,
        knowledge_base=knowledge_base,
        owner="knowledge_maintenance-anomaly-worker",
    )

    vectors = {}
    if anomaly in {"count_mismatch", "dimension_mismatch"}:
        vectors["expected-vector"] = {
            "knowledge_base_id": knowledge_base.id,
            "document_uuid": "expected-uuid",
        }

    async def get_items(_collection_name, offset=0, limit=None, include=None):
        del include
        rows = list(vectors.items())
        rows = rows[offset:] if limit is None else rows[offset : offset + limit]
        return {
            "ids": [item_id for item_id, _metadata in rows],
            "metadatas": [metadata for _item_id, metadata in rows],
        }

    async def get_items_by_ids(_collection_name, ids, include=None):
        del include
        return {"ids": [item_id for item_id in ids if item_id in vectors]}

    async def validate(
        _collection_name,
        expected_count=None,
        expected_metadata=None,
        expected_dimension=None,
        sample_size=1,
    ) -> CollectionValidationResult:
        del expected_metadata, expected_dimension, sample_size
        if anomaly == "missing_collection":
            return CollectionValidationResult(exists=False, valid=False, count=0, errors=("collection_missing",))
        if expected_count is None:
            return CollectionValidationResult(exists=True, valid=True, count=len(vectors), errors=())
        error = "dimension_mismatch" if anomaly == "dimension_mismatch" else "count_mismatch"
        return CollectionValidationResult(
            exists=True,
            valid=False,
            count=len(vectors) + (1 if anomaly == "count_mismatch" else 0),
            errors=(error,),
        )

    monkeypatch.setattr(maintenance_module, "async_get_collection_items", get_items)
    monkeypatch.setattr(maintenance_module, "async_get_collection_items_by_ids", get_items_by_ids)
    monkeypatch.setattr(maintenance_module, "async_validate_collection", validate)

    result = await handle_knowledge_maintenance(
        KnowledgeJobExecutionContext(
            job=claimed,
            worker_id="knowledge_maintenance-anomaly-worker",
            session_factory=session_factory,
        )
    )

    assert result.result["consistent"] is False
    assert result.result["repair_required"] is True

    async with session_factory() as db:
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base.id)
        migration_jobs = list(
            (
                await db.execute(
                    select(KnowledgeJob).where(
                        KnowledgeJob.knowledge_base_id == knowledge_base.id,
                        KnowledgeJob.operation == KnowledgeJobOperation.EMBEDDING_MIGRATION,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert current_knowledge_base is not None
    assert current_knowledge_base.index_status == KnowledgeBaseIndexStatus.FAILED
    assert migration_jobs == []
