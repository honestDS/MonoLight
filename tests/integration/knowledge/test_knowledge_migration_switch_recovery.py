from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.knowledge_jobs.migration_build as migration_build_module
import app.core.knowledge_jobs.migration_prepare as migration_prepare_module
import app.core.knowledge_jobs.migration_switch as migration_switch_module
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.knowledge.migration import record_knowledge_base_migration_change
from app.core.knowledge_jobs.executor import KnowledgeJobExecutionContext, KnowledgeJobLeaseLostError, KnowledgeJobRetryableError
from app.core.knowledge_jobs.handlers import create_default_knowledge_job_executor
from app.core.knowledge_jobs.manager import KnowledgeJobTargetBusyError
from app.core.knowledge_jobs.migration import (
    prepare_knowledge_base_embedding_migration,
)
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseDocument,
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseMigrationStatus,
    KnowledgeBaseOldCollectionCleanupStatus,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeItem,
)
from app.providers.database.time import get_database_time
from app.providers.vector.chroma import CollectionValidationResult
from tests.integration.knowledge import test_knowledge_migration as migration_tests
from tests.integration.knowledge.test_knowledge_migration import (
    _claim,
    _create_container,
    _patch_migration_backend,
    _VectorBackend,
)

migration_database = migration_tests.migration_database


@pytest.mark.asyncio
async def test_switch_transaction_failure_keeps_old_collection_active(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, backend)
    kb = await _create_container(migration_database, uid="switch-failure-user")
    async with migration_database() as db:
        document = KnowledgeBaseDocument(
            knowledge_base_id=kb.id,
            filename="switch.txt",
            content="switch transaction content",
            chunk_size=10,
            chunk_overlap=2,
            batch_size=2,
            chunk_count=1,
            chunk_ids=["legacy-switch-vector"],
            metadata_={},
        )
        db.add(document)
        await db.commit()
        await db.refresh(document)
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="knowledge_migration-switch-failure-target",
            dedupe_key="knowledge_migration-switch-failure-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="knowledge_migration-switch-failure-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="knowledge_migration-switch-failure-worker",
        session_factory=migration_database,
    )
    payload = await migration_prepare_module._prepare_migration(context, claimed.payload)
    await migration_build_module._build_migration(context, payload)
    await migration_build_module._catch_up_migration(context, payload)
    validation = await migration_switch_module._validate_migration(context, payload)

    async def _reject_success(*args, **kwargs):
        return False

    monkeypatch.setattr(knowledge_job_crud, "mark_succeeded", _reject_success)
    with pytest.raises(KnowledgeJobLeaseLostError):
        await migration_switch_module._switch_migration(context, payload, validation)

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        current_document = await db.get(KnowledgeBaseDocument, document.id)
        cleanup_job = await db.scalar(
            select(KnowledgeJob).where(
                KnowledgeJob.parent_job_id == job.id,
                KnowledgeJob.operation == KnowledgeJobOperation.OLD_COLLECTION_CLEANUP,
            )
        )
    assert current is not None
    assert current.active_collection_name == kb.active_collection_name
    assert current.active_embedding_model_id == kb.active_embedding_model_id
    assert current.migration_status == KnowledgeBaseMigrationStatus.VALIDATING
    assert current.target_collection_name == "knowledge_migration-switch-failure-target"
    assert current_document is not None
    assert current_document.chunk_ids == ["legacy-switch-vector"]
    assert cleanup_job is None


@pytest.mark.asyncio
async def test_switch_reuses_validated_plan_without_reloading_sources_under_lock(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, backend)
    kb = await _create_container(migration_database, uid="short-switch-user")
    async with migration_database() as db:
        document = KnowledgeBaseDocument(
            knowledge_base_id=kb.id,
            filename="short-switch.txt",
            content="validated plan should be reused during final switch",
            chunk_size=10,
            chunk_overlap=2,
            batch_size=2,
            chunk_count=1,
            chunk_ids=["legacy-short-switch-vector"],
            metadata_={},
        )
        db.add(document)
        await db.commit()
        await db.refresh(document)
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="knowledge_migration-short-switch-target",
            dedupe_key="knowledge_migration-short-switch-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="knowledge_migration-short-switch-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="knowledge_migration-short-switch-worker",
        session_factory=migration_database,
    )
    payload = await migration_prepare_module._prepare_migration(context, claimed.payload)
    await migration_build_module._build_migration(context, payload)
    await migration_build_module._catch_up_migration(context, payload)
    validation = await migration_switch_module._validate_migration(context, payload)

    async def _unexpected_reload(*args, **kwargs):
        raise AssertionError("final switch must not reload and re-split all knowledge sources")

    monkeypatch.setattr(
        migration_switch_module.knowledge_base_migration_crud,
        "list_current_sources",
        _unexpected_reload,
    )
    execution = await migration_switch_module._switch_migration(context, payload, validation)

    assert execution.finalized is True
    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        current_document = await db.get(KnowledgeBaseDocument, document.id)
    assert current is not None
    assert current.active_collection_name == "knowledge_migration-short-switch-target"
    assert current.migration_status == KnowledgeBaseMigrationStatus.SUCCEEDED
    assert current_document is not None
    assert current_document.chunk_ids == list(validation.plans[0].item_ids)


@pytest.mark.asyncio
async def test_switch_batches_managed_vector_reference_updates(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, backend)
    kb = await _create_container(
        migration_database,
        uid="managed-switch-user",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
    )
    async with migration_database() as db:
        first = ManagedKnowledgeItem(
            knowledge_base_id=kb.id,
            uid=kb.uid,
            knowledge_key="managed.switch.first",
            content="first managed migration source",
            content_hash="1" * 64,
            version=1,
            indexed_version=1,
            vector_item_ids=["legacy-managed-first"],
            is_recallable=True,
        )
        second = ManagedKnowledgeItem(
            knowledge_base_id=kb.id,
            uid=kb.uid,
            knowledge_key="managed.switch.second",
            content="second managed migration source",
            content_hash="2" * 64,
            version=1,
            indexed_version=1,
            vector_item_ids=["legacy-managed-second"],
            is_recallable=True,
        )
        db.add_all([first, second])
        await db.commit()
        await db.refresh(first)
        await db.refresh(second)
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="knowledge_migration-managed-switch-target",
            dedupe_key="knowledge_migration-managed-switch-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="knowledge_migration-managed-switch-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="knowledge_migration-managed-switch-worker",
        session_factory=migration_database,
    )
    execution = await migration_module.handle_embedding_migration(context)
    assert execution.finalized is True

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        migrated_first = await db.get(ManagedKnowledgeItem, first.id)
        migrated_second = await db.get(ManagedKnowledgeItem, second.id)
    assert current is not None
    assert current.active_collection_name == "knowledge_migration-managed-switch-target"
    assert current.active_embedding_model_id == "embedding-v2"
    assert current.active_embedding_dimensions == 3
    assert current.active_embedding_signature == "signature-v2"
    assert migrated_first is not None and migrated_first.indexed_version == 1
    assert migrated_second is not None and migrated_second.indexed_version == 1
    assert migrated_first.vector_item_ids != ["legacy-managed-first"]
    assert migrated_second.vector_item_ids != ["legacy-managed-second"]
    target_ids = set(backend.collections["knowledge_migration-managed-switch-target"]["items"])
    assert set(migrated_first.vector_item_ids).issubset(target_ids)
    assert set(migrated_second.vector_item_ids).issubset(target_ids)


@pytest.mark.asyncio
async def test_switch_rejects_validated_plan_when_delta_arrives_before_lock(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, backend)
    kb = await _create_container(migration_database, uid="switch-fence-user")
    async with migration_database() as db:
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="knowledge_migration-switch-fence-target",
            dedupe_key="knowledge_migration-switch-fence-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="knowledge_migration-switch-fence-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="knowledge_migration-switch-fence-worker",
        session_factory=migration_database,
    )
    payload = await migration_prepare_module._prepare_migration(context, claimed.payload)
    await migration_build_module._build_migration(context, payload)
    await migration_build_module._catch_up_migration(context, payload)
    validation = await migration_switch_module._validate_migration(context, payload)

    async with migration_database() as db:
        locked = await migration_module.lock_migrating_knowledge_base(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
        )
        assert locked is not None
        document = KnowledgeBaseDocument(
            knowledge_base_id=kb.id,
            filename="late-switch.txt",
            content="late write after validation",
            chunk_size=10,
            chunk_overlap=2,
            batch_size=2,
            chunk_count=0,
            chunk_ids=[],
            metadata_={},
        )
        db.add(document)
        await db.flush()
        await record_knowledge_base_migration_change(
            db,
            knowledge_base=locked,
            source_type=KnowledgeBaseMigrationSourceType.USER_DOCUMENT,
            source_id=document.id,
            action=KnowledgeBaseMigrationDeltaAction.UPSERT,
        )
        await db.commit()

    with pytest.raises(KnowledgeJobRetryableError):
        await migration_switch_module._switch_migration(context, payload, validation)

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
    assert current is not None
    assert current.active_collection_name == kb.active_collection_name
    assert current.migration_status == KnowledgeBaseMigrationStatus.CATCHING_UP
    assert current.migration_delta_high_watermark == 1
    assert current.migration_delta_applied_watermark == 0


@pytest.mark.asyncio
async def test_validation_failure_never_switches_active_collection(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, backend)
    kb = await _create_container(migration_database, uid="validation-user")

    async with migration_database() as db:
        document = KnowledgeBaseDocument(
            knowledge_base_id=kb.id,
            filename="validation.txt",
            content="validation content",
            chunk_size=10,
            chunk_overlap=2,
            batch_size=2,
            chunk_count=0,
            chunk_ids=[],
            metadata_={},
        )
        db.add(document)
        await db.commit()
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="knowledge_migration-validation-target",
            dedupe_key="knowledge_migration-validation-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="knowledge_migration-validation-worker",
    )

    original_validate = backend.validate

    async def _invalid_validate(*args, **kwargs) -> CollectionValidationResult:
        result = await original_validate(*args, **kwargs)
        return CollectionValidationResult(
            exists=result.exists,
            valid=False,
            count=result.count,
            metadata=result.metadata,
            sample_dimension=result.sample_dimension,
            errors=("forced_validation_failure",),
        )

    monkeypatch.setattr(migration_switch_module, "async_validate_collection", _invalid_validate)
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="knowledge_migration-validation-worker",
        session_factory=migration_database,
    )
    with pytest.raises(KnowledgeJobRetryableError):
        await migration_module.handle_embedding_migration(context)

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
    assert current is not None
    assert current.active_collection_name == kb.active_collection_name
    assert current.target_collection_name == "knowledge_migration-validation-target"
    assert current.migration_status == KnowledgeBaseMigrationStatus.VALIDATING


@pytest.mark.asyncio
async def test_old_collection_cleanup_failure_is_independent_from_migration_success(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, backend)
    kb = await _create_container(migration_database, uid="cleanup-user")
    await backend.get_or_create(kb.active_collection_name)
    backend.delete_failures[kb.active_collection_name] = 1

    async with migration_database() as db:
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="knowledge_migration-cleanup-target",
            dedupe_key="knowledge_migration-cleanup-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="knowledge_migration-cleanup-migration-worker",
    )
    executor = create_default_knowledge_job_executor(session_factory=migration_database)
    migration_result = await executor.execute_claimed(
        claimed,
        "knowledge_migration-cleanup-migration-worker",
    )
    assert migration_result.finalized is True

    async with migration_database() as db:
        cleanup_job = await db.scalar(
            select(KnowledgeJob).where(
                KnowledgeJob.parent_job_id == job.id,
                KnowledgeJob.operation == KnowledgeJobOperation.OLD_COLLECTION_CLEANUP,
            )
        )
    assert cleanup_job is not None and cleanup_job.id is not None

    cleanup_claim = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=cleanup_job.id,
        owner="knowledge_migration-cleanup-worker",
    )
    cleanup_context = KnowledgeJobExecutionContext(
        job=cleanup_claim,
        worker_id="knowledge_migration-cleanup-worker",
        session_factory=migration_database,
    )
    with pytest.raises(KnowledgeJobRetryableError):
        await migration_module.handle_old_collection_cleanup(cleanup_context)

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
    assert current is not None
    assert current.active_collection_name == "knowledge_migration-cleanup-target"
    assert current.migration_status == KnowledgeBaseMigrationStatus.SUCCEEDED
    assert current.old_collection_cleanup_status == KnowledgeBaseOldCollectionCleanupStatus.FAILED

    async with migration_database() as db:
        with pytest.raises(KnowledgeJobTargetBusyError):
            await prepare_knowledge_base_embedding_migration(
                db,
                uid=kb.uid,
                knowledge_base_id=kb.id,
                target_channel_id=kb.active_embedding_channel_id,
                target_model_id="embedding-v3",
                target_dimensions=3,
                target_signature="signature-v3",
                target_collection_name="knowledge_migration-cleanup-next-target",
                dedupe_key="knowledge_migration-cleanup-next-migration",
            )
        await db.rollback()
        assert await knowledge_job_crud.release_for_retry(
            db,
            uid=kb.uid,
            job_id=cleanup_job.id,
            owner="knowledge_migration-cleanup-worker",
            error="retry cleanup",
            delay_seconds=0,
        )

    cleanup_retry_claim = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=cleanup_job.id,
        owner="knowledge_migration-cleanup-retry-worker",
    )
    cleanup_retry_context = KnowledgeJobExecutionContext(
        job=cleanup_retry_claim,
        worker_id="knowledge_migration-cleanup-retry-worker",
        session_factory=migration_database,
    )
    cleanup_result = await migration_module.handle_old_collection_cleanup(cleanup_retry_context)
    assert cleanup_result.finalized is True

    async with migration_database() as db:
        next_job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v3",
            target_dimensions=3,
            target_signature="signature-v3",
            target_collection_name="knowledge_migration-cleanup-next-target",
            dedupe_key="knowledge_migration-cleanup-next-migration",
        )
    assert next_job.id is not None


@pytest.mark.asyncio
async def test_worker_lease_expiry_resumes_migration_even_at_attempt_limit(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, backend)
    kb = await _create_container(migration_database, uid="restart-user")
    async with migration_database() as db:
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="knowledge_migration-restart-target",
            dedupe_key="knowledge_migration-restart-migration",
            max_attempts=1,
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="knowledge_migration-restart-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="knowledge_migration-restart-worker",
        session_factory=migration_database,
    )
    await migration_prepare_module._prepare_migration(context, claimed.payload)

    async with migration_database() as db:
        current_job = await knowledge_job_crud.get_by_id(db, uid=kb.uid, job_id=job.id)
        assert current_job is not None
        current_job.lock_until = await get_database_time(db) - timedelta(seconds=1)
        db.add(current_job)
        await db.commit()
        recovery = await knowledge_job_crud.recover_expired(
            db,
            delay_seconds=0,
            max_attempts_error="should-not-fail",
        )
        resumed = await knowledge_job_crud.get_by_id(db, uid=kb.uid, job_id=job.id)
        current_kb = await db.get(KnowledgeBase, kb.id)

    assert recovery.retried == 1
    assert recovery.failed == 0
    assert resumed is not None and resumed.status == KnowledgeJobStatus.RETRY
    assert current_kb is not None
    assert current_kb.migration_status == KnowledgeBaseMigrationStatus.BUILDING
    assert current_kb.target_collection_name == "knowledge_migration-restart-target"
    reclaimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="knowledge_migration-restart-worker-2",
    )
    assert reclaimed.attempt_count == 2


@pytest.mark.asyncio
async def test_cancelled_migration_keeps_old_collection_and_cleans_target(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, backend)
    kb = await _create_container(migration_database, uid="cancel-user")
    async with migration_database() as db:
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="knowledge_migration-cancel-target",
            dedupe_key="knowledge_migration-cancel-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="knowledge_migration-cancel-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="knowledge_migration-cancel-worker",
        session_factory=migration_database,
    )
    await migration_prepare_module._prepare_migration(context, claimed.payload)
    assert "knowledge_migration-cancel-target" in backend.collections

    async with migration_database() as db:
        released = await knowledge_job_crud.release_for_retry(
            db,
            uid=kb.uid,
            job_id=job.id,
            owner="knowledge_migration-cancel-worker",
            error="retry",
            delay_seconds=0,
        )
    assert released is True

    async with migration_database() as db:
        cancellation = await migration_module.cancel_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
        )
    assert cancellation.accepted is True
    assert cancellation.changed is True

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        cancelled_job = await knowledge_job_crud.get_by_id(
            db,
            uid=kb.uid,
            job_id=job.id,
        )
        cleanup_job = await db.scalar(
            select(KnowledgeJob).where(
                KnowledgeJob.parent_job_id == job.id,
                KnowledgeJob.operation == KnowledgeJobOperation.MIGRATION_TARGET_CLEANUP,
            )
        )
    assert current is not None
    assert current.active_collection_name == kb.active_collection_name
    assert current.migration_status == KnowledgeBaseMigrationStatus.CANCELLED
    assert current.target_collection_name is None
    assert cancelled_job is not None and cancelled_job.status == KnowledgeJobStatus.CANCELLED
    assert cleanup_job is not None and cleanup_job.id is not None
    assert "knowledge_migration-cancel-target" in backend.collections

    cleanup_claim = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=cleanup_job.id,
        owner="knowledge_migration-cancel-cleanup-worker",
    )
    cleanup_context = KnowledgeJobExecutionContext(
        job=cleanup_claim,
        worker_id="knowledge_migration-cancel-cleanup-worker",
        session_factory=migration_database,
    )
    cleanup_result = await migration_module.handle_migration_target_cleanup(cleanup_context)
    assert cleanup_result.result["collection"] == "knowledge_migration-cancel-target"
    assert "knowledge_migration-cancel-target" not in backend.collections
