from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.knowledge.embedding_migration import submit_managed_knowledge_base_migrations_for_memory_revision
from app.core.knowledge.managed import managed_knowledge_service
from app.core.knowledge.migration import record_knowledge_base_migration_change
from app.core.knowledge_jobs.consumer import KnowledgeJobConsumer
from app.core.knowledge_jobs.executor import KnowledgeJobExecutionContext, KnowledgeJobExecutor, KnowledgeJobLeaseLostError, KnowledgeJobRetryableError
from app.core.knowledge_jobs.handlers import create_default_knowledge_job_executor
from app.core.knowledge_jobs.manager import KnowledgeJobTargetBusyError
from app.core.knowledge_jobs.migration import (
    prepare_knowledge_base_embedding_migration,
)
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseDocument,
    KnowledgeBaseEmbeddingDelta,
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationDeltaStatus,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseMigrationStatus,
    KnowledgeBaseOldCollectionCleanupStatus,
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
    KnowledgeBaseCollectionOwner.__table__,
    KnowledgeBaseDocument.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeJob.__table__,
    KnowledgeBaseEmbeddingDelta.__table__,
)


@pytest_asyncio.fixture
async def migration_database(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "knowledge-migration-stage7.db"
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
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=_TABLES,
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


class _VectorBackend:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, object]] = {}
        self.delete_failures: dict[str, int] = {}

    async def get_or_create(self, name: str, metadata=None, distance=None):
        collection = self.collections.setdefault(
            name,
            {"metadata": dict(metadata or {}), "items": {}},
        )
        if metadata:
            collection["metadata"] = dict(metadata)
        return collection

    async def upsert(
        self,
        name: str,
        item_ids,
        documents,
        embeddings,
        metadatas,
        batch_size=100,
    ) -> int:
        del batch_size
        collection = self.collections.setdefault(
            name,
            {"metadata": {}, "items": {}},
        )
        items = collection["items"]
        assert isinstance(items, dict)
        for item_id, document, embedding, metadata in zip(
            item_ids,
            documents,
            embeddings,
            metadatas,
            strict=True,
        ):
            items[item_id] = {
                "document": document,
                "embedding": list(embedding),
                "metadata": dict(metadata),
            }
        return len(item_ids)

    async def delete_items(self, name: str, item_ids, batch_size=100) -> int:
        del batch_size
        collection = self.collections.get(name)
        if collection is None:
            return 0
        items = collection["items"]
        assert isinstance(items, dict)
        for item_id in item_ids:
            items.pop(item_id, None)
        return len(item_ids)

    async def list_items(self, name: str, offset=0, limit=None, include=None):
        del include
        collection = self.collections.get(name, {"items": {}})
        items = collection["items"]
        assert isinstance(items, dict)
        rows = list(items.items())
        rows = rows[offset:] if limit is None else rows[offset : offset + limit]
        return {
            "ids": [item_id for item_id, _ in rows],
            "documents": [item["document"] for _, item in rows],
            "metadatas": [item["metadata"] for _, item in rows],
            "embeddings": [item["embedding"] for _, item in rows],
        }

    async def validate(
        self,
        name: str,
        expected_count=None,
        expected_metadata=None,
        expected_dimension=None,
        sample_size=1,
    ):
        del sample_size
        collection = self.collections.get(name)
        if collection is None:
            return SimpleNamespace(
                exists=False,
                valid=False,
                count=None,
                metadata=None,
                sample_dimension=None,
                errors=("collection_not_found",),
            )
        items = collection["items"]
        metadata = collection["metadata"]
        assert isinstance(items, dict)
        assert isinstance(metadata, dict)
        errors: list[str] = []
        if expected_count is not None and len(items) != expected_count:
            errors.append("count_mismatch")
        for key, value in (expected_metadata or {}).items():
            if metadata.get(key) != value:
                errors.append(f"metadata_mismatch:{key}")
        sample_dimension = None
        if items:
            sample_dimension = len(next(iter(items.values()))["embedding"])
            if expected_dimension is not None and sample_dimension != expected_dimension:
                errors.append("dimension_mismatch")
        elif expected_dimension is not None and expected_count:
            errors.append("sample_dimension_missing")
        return SimpleNamespace(
            exists=True,
            valid=not errors,
            count=len(items),
            metadata=dict(metadata),
            sample_dimension=sample_dimension,
            errors=tuple(errors),
        )

    async def query(self, name: str, query_embedding, n_results=1, include=None):
        del query_embedding, include
        collection = self.collections.get(name, {"items": {}})
        items = collection["items"]
        assert isinstance(items, dict)
        item_ids = list(items)[:n_results]
        return {"ids": [item_ids]}

    async def delete_collection(self, name: str) -> None:
        remaining = self.delete_failures.get(name, 0)
        if remaining > 0:
            self.delete_failures[name] = remaining - 1
            raise RuntimeError("delete failed")
        self.collections.pop(name, None)


async def _create_container(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str = "user-1",
    knowledge_base_type: KnowledgeBaseType = KnowledgeBaseType.USER,
) -> KnowledgeBase:
    async with session_factory() as db:
        channel = ModelChannel(
            name=f"embedding-{uid}",
            api_key="enc:v1:test-api-key",
            base_url="https://embedding.invalid/v1",
            model_ids=[],
        )
        db.add(channel)
        await db.flush()

        managed_profile_id = None
        if knowledge_base_type == KnowledgeBaseType.LLM_MANAGED:
            library = PromptLibrary(name=f"prompts-{uid}", uid=uid, content="prompt")
            db.add(library)
            await db.flush()
            profile = Profile(
                name=f"profile-{uid}",
                uid=uid,
                prompt_id=library.id,
                configs={},
            )
            db.add(profile)
            await db.flush()
            managed_profile_id = profile.id

        kb = KnowledgeBase(
            uid=uid,
            name="stage7",
            embedding_channel_id=channel.id,
            embedding_model_id="embedding-v1",
            embedding_dimensions=2,
            collection_name=f"kb-{uid}-legacy",
            knowledge_base_type=knowledge_base_type,
            managed_profile_id=managed_profile_id,
            active_embedding_channel_id=channel.id,
            active_embedding_model_id="embedding-v1",
            active_embedding_dimensions=2,
            active_embedding_signature="signature-v1",
            active_embedding_revision=1,
            active_collection_name=f"kb-{uid}-active",
            index_revision=1,
        )
        db.add(kb)
        await db.commit()
        await db.refresh(kb)
        return kb


async def _claim(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
    owner: str,
) -> KnowledgeJob:
    async with session_factory() as db:
        claimed = await knowledge_job_crud.try_claim(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            lease_seconds=60,
        )
    assert claimed is not None
    return claimed


def _patch_migration_backend(monkeypatch, migration_module, backend: _VectorBackend) -> None:
    async def _load_config(_db, channel_id, model_id):
        return SimpleNamespace(
            channel_id=channel_id,
            model_id=model_id,
            declared_dimensions=3,
        )

    async def _embed(_config, texts, batch_size=16, dimensions=None, **_kwargs):
        del batch_size
        size = dimensions or 3
        return [[float(index + 1) for index in range(size)] for _ in texts]

    monkeypatch.setattr(migration_module, "load_embedding_runtime_config", _load_config)
    monkeypatch.setattr(migration_module, "embed_texts_with_config", _embed)
    monkeypatch.setattr(migration_module, "async_get_or_create_collection", backend.get_or_create)
    monkeypatch.setattr(migration_module, "async_upsert_collection_items", backend.upsert)
    monkeypatch.setattr(migration_module, "async_delete_collection_items", backend.delete_items)
    monkeypatch.setattr(migration_module, "async_get_collection_items", backend.list_items)
    monkeypatch.setattr(migration_module, "async_validate_collection", backend.validate)
    monkeypatch.setattr(migration_module, "async_query_collection", backend.query)
    monkeypatch.setattr(migration_module, "async_delete_collection", backend.delete_collection)


@pytest.mark.asyncio
async def test_migration_rebuilds_user_documents_and_switches_only_after_validation(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
    kb = await _create_container(migration_database)

    async with migration_database() as db:
        document = KnowledgeBaseDocument(
            knowledge_base_id=kb.id,
            filename="guide.txt",
            content="alpha beta gamma",
            chunk_size=8,
            chunk_overlap=2,
            batch_size=2,
            chunk_count=1,
            chunk_ids=["legacy-doc-vector"],
            metadata_={"document_uuid": "legacy-doc"},
        )
        db.add(document)
        await db.commit()

    async with migration_database() as db:
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="stage7-user-target",
            dedupe_key="stage7-user-migration",
        )

    assert job.id is not None
    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        assert current is not None
        assert resolve_active_knowledge_base_embedding(current).collection_name == kb.active_collection_name
        assert current.target_collection_name == "stage7-user-target"
        assert current.migration_status == KnowledgeBaseMigrationStatus.PREPARING

    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-worker",
    )
    executor = create_default_knowledge_job_executor(session_factory=migration_database)
    execution = await executor.execute_claimed(claimed, "stage7-worker")
    assert execution.finalized is True

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        migrated_document = await db.get(KnowledgeBaseDocument, document.id)
        migration_job = await knowledge_job_crud.get_by_id(
            db,
            uid=kb.uid,
            job_id=job.id,
        )
        cleanup_job = await db.scalar(
            select(KnowledgeJob).where(
                KnowledgeJob.parent_job_id == job.id,
                KnowledgeJob.operation == KnowledgeJobOperation.OLD_COLLECTION_CLEANUP,
            )
        )

    assert current is not None
    assert current.active_collection_name == "stage7-user-target"
    assert current.active_embedding_model_id == "embedding-v2"
    assert current.active_embedding_dimensions == 3
    assert current.migration_status == KnowledgeBaseMigrationStatus.SUCCEEDED
    assert current.old_collection_name == kb.active_collection_name
    assert current.old_collection_cleanup_status == KnowledgeBaseOldCollectionCleanupStatus.PENDING
    assert migration_job is not None and migration_job.status == KnowledgeJobStatus.SUCCEEDED
    assert cleanup_job is not None and cleanup_job.status == KnowledgeJobStatus.PENDING
    assert migrated_document is not None
    assert migrated_document.chunk_ids
    assert all(item_id.startswith(f"kbm_doc_{kb.id}_{document.id}_r2_") for item_id in migrated_document.chunk_ids)
    target_items = backend.collections["stage7-user-target"]["items"]
    assert isinstance(target_items, dict)
    assert set(target_items) == set(migrated_document.chunk_ids)


@pytest.mark.asyncio
async def test_migration_applies_monotonic_delta_for_new_document_after_snapshot(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
    kb = await _create_container(migration_database, uid="delta-user")

    async with migration_database() as db:
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="stage7-delta-target",
            dedupe_key="stage7-delta-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-delta-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="stage7-delta-worker",
        session_factory=migration_database,
    )
    await migration_module._prepare_migration(context, claimed.payload)

    async with migration_database() as db:
        locked = await migration_module.lock_migrating_knowledge_base(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
        )
        assert locked is not None
        document = KnowledgeBaseDocument(
            knowledge_base_id=kb.id,
            filename="late.txt",
            content="late document content",
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

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        deltas = list((await db.execute(select(KnowledgeBaseEmbeddingDelta).where(KnowledgeBaseEmbeddingDelta.migration_job_id == job.id))).scalars().all())
    assert current is not None
    assert current.migration_delta_high_watermark == 1
    assert [delta.sequence for delta in deltas] == [1]
    assert deltas[0].status == KnowledgeBaseMigrationDeltaStatus.PENDING

    execution = await migration_module.handle_embedding_migration(context)
    assert execution.finalized is True

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        migrated_document = await db.get(KnowledgeBaseDocument, document.id)
        delta = await db.scalar(
            select(KnowledgeBaseEmbeddingDelta).where(
                KnowledgeBaseEmbeddingDelta.migration_job_id == job.id,
                KnowledgeBaseEmbeddingDelta.sequence == 1,
            )
        )
    assert current is not None
    assert current.migration_delta_applied_watermark == 1
    assert migrated_document is not None
    assert set(migrated_document.chunk_ids).issubset(set(backend.collections["stage7-delta-target"]["items"]))
    assert delta is not None and delta.status == KnowledgeBaseMigrationDeltaStatus.APPLIED


@pytest.mark.asyncio
async def test_managed_knowledge_mutation_automatically_records_migration_delta(
    migration_database: async_sessionmaker[AsyncSession],
) -> None:
    kb = await _create_container(
        migration_database,
        uid="managed-delta-user",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
    )
    async with migration_database() as db:
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="stage7-managed-target",
            dedupe_key="stage7-managed-migration",
        )
        mutation = await managed_knowledge_service.create(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            knowledge_key="managed.delta",
            content="managed content written while migration is active",
            source_type=ManagedKnowledgeSourceType.USER_API,
            actor=ManagedKnowledgeActorType.USER,
        )

    assert job.id is not None
    assert mutation.item is not None and mutation.item.id is not None
    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        delta = await db.scalar(
            select(KnowledgeBaseEmbeddingDelta).where(
                KnowledgeBaseEmbeddingDelta.migration_job_id == job.id,
                KnowledgeBaseEmbeddingDelta.sequence == 1,
            )
        )
    assert current is not None
    assert current.migration_delta_high_watermark == 1
    assert delta is not None
    assert delta.source_type == KnowledgeBaseMigrationSourceType.MANAGED_KNOWLEDGE
    assert delta.source_id == mutation.item.id
    assert delta.action == KnowledgeBaseMigrationDeltaAction.UPSERT


@pytest.mark.asyncio
async def test_managed_update_and_delete_record_monotonic_migration_deltas(
    migration_database: async_sessionmaker[AsyncSession],
) -> None:
    kb = await _create_container(
        migration_database,
        uid="managed-update-delete-user",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
    )
    async with migration_database() as db:
        created = await managed_knowledge_service.create(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            knowledge_key="managed.change",
            content="managed version one",
            source_type=ManagedKnowledgeSourceType.USER_API,
            actor=ManagedKnowledgeActorType.USER,
        )
    assert created.item is not None and created.item.id is not None

    async with migration_database() as db:
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="stage7-managed-change-target",
            dedupe_key="stage7-managed-change-migration",
        )
        updated = await managed_knowledge_service.update(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            knowledge_id=created.item.id,
            expected_version=1,
            knowledge_key="managed.change",
            content="managed version two",
            source_type=ManagedKnowledgeSourceType.USER_API,
            actor=ManagedKnowledgeActorType.USER,
        )
        assert updated.item is not None
        updated_version = updated.item.version
        deleted = await managed_knowledge_service.delete(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            knowledge_id=created.item.id,
            expected_version=updated_version,
            source_type=ManagedKnowledgeSourceType.USER_API,
            actor=ManagedKnowledgeActorType.USER,
        )
    assert job.id is not None
    assert deleted.item is not None

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        deltas = list((await db.execute(select(KnowledgeBaseEmbeddingDelta).where(KnowledgeBaseEmbeddingDelta.migration_job_id == job.id).order_by(KnowledgeBaseEmbeddingDelta.sequence.asc()))).scalars().all())
    assert current is not None
    assert current.migration_delta_high_watermark == 2
    assert [delta.sequence for delta in deltas] == [1, 2]
    assert [delta.action for delta in deltas] == [
        KnowledgeBaseMigrationDeltaAction.UPSERT,
        KnowledgeBaseMigrationDeltaAction.DELETE,
    ]
    assert [delta.source_version for delta in deltas] == [
        updated_version,
        deleted.item.version,
    ]


@pytest.mark.asyncio
async def test_delta_arriving_during_validation_returns_to_catchup(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
    kb = await _create_container(migration_database, uid="validation-delta-user")
    async with migration_database() as db:
        job = await prepare_knowledge_base_embedding_migration(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="signature-v2",
            target_collection_name="stage7-validation-delta-target",
            dedupe_key="stage7-validation-delta-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-validation-delta-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="stage7-validation-delta-worker",
        session_factory=migration_database,
    )
    payload = await migration_module._prepare_migration(context, claimed.payload)
    await migration_module._build_migration(context, payload)
    await migration_module._catch_up_migration(context, payload)

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        assert current is not None
        assert current.migration_status == KnowledgeBaseMigrationStatus.VALIDATING
        locked = await migration_module.lock_migrating_knowledge_base(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
        )
        assert locked is not None
        document = KnowledgeBaseDocument(
            knowledge_base_id=kb.id,
            filename="late-validation.txt",
            content="late validation content",
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

    execution = await migration_module.handle_embedding_migration(context)
    assert execution.finalized is True
    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        migrated_document = await db.get(KnowledgeBaseDocument, document.id)
    assert current is not None
    assert current.migration_status == KnowledgeBaseMigrationStatus.SUCCEEDED
    assert current.migration_delta_applied_watermark == 1
    assert migrated_document is not None
    assert set(migrated_document.chunk_ids).issubset(set(backend.collections["stage7-validation-delta-target"]["items"]))


@pytest.mark.asyncio
async def test_document_delete_after_snapshot_is_removed_from_target(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
    kb = await _create_container(migration_database, uid="delete-delta-user")
    async with migration_database() as db:
        document = KnowledgeBaseDocument(
            knowledge_base_id=kb.id,
            filename="delete-me.txt",
            content="content deleted during migration",
            chunk_size=10,
            chunk_overlap=2,
            batch_size=2,
            chunk_count=1,
            chunk_ids=["legacy-delete-vector"],
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
            target_collection_name="stage7-delete-target",
            dedupe_key="stage7-delete-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-delete-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="stage7-delete-worker",
        session_factory=migration_database,
    )
    payload = await migration_module._prepare_migration(context, claimed.payload)
    await migration_module._build_migration(context, payload)
    assert backend.collections["stage7-delete-target"]["items"]

    async with migration_database() as db:
        locked = await migration_module.lock_migrating_knowledge_base(
            db,
            uid=kb.uid,
            knowledge_base_id=kb.id,
        )
        current_document = await db.get(KnowledgeBaseDocument, document.id)
        assert locked is not None and current_document is not None
        await db.delete(current_document)
        await db.flush()
        await record_knowledge_base_migration_change(
            db,
            knowledge_base=locked,
            source_type=KnowledgeBaseMigrationSourceType.USER_DOCUMENT,
            source_id=document.id,
            action=KnowledgeBaseMigrationDeltaAction.DELETE,
        )
        await db.commit()

    execution = await migration_module.handle_embedding_migration(context)
    assert execution.finalized is True
    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        deleted_document = await db.get(KnowledgeBaseDocument, document.id)
    assert current is not None
    assert current.migration_status == KnowledgeBaseMigrationStatus.SUCCEEDED
    assert current.migration_delta_applied_watermark == 1
    assert deleted_document is None
    assert backend.collections["stage7-delete-target"]["items"] == {}


@pytest.mark.asyncio
async def test_switch_transaction_failure_keeps_old_collection_active(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
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
            target_collection_name="stage7-switch-failure-target",
            dedupe_key="stage7-switch-failure-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-switch-failure-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="stage7-switch-failure-worker",
        session_factory=migration_database,
    )
    payload = await migration_module._prepare_migration(context, claimed.payload)
    await migration_module._build_migration(context, payload)
    await migration_module._catch_up_migration(context, payload)
    validation = await migration_module._validate_migration(context, payload)

    async def _reject_success(*args, **kwargs):
        return False

    monkeypatch.setattr(knowledge_job_crud, "mark_succeeded", _reject_success)
    with pytest.raises(KnowledgeJobLeaseLostError):
        await migration_module._switch_migration(context, payload, validation)

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
    assert current.target_collection_name == "stage7-switch-failure-target"
    assert current_document is not None
    assert current_document.chunk_ids == ["legacy-switch-vector"]
    assert cleanup_job is None


@pytest.mark.asyncio
async def test_switch_reuses_validated_plan_without_reloading_sources_under_lock(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
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
            target_collection_name="stage7-short-switch-target",
            dedupe_key="stage7-short-switch-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-short-switch-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="stage7-short-switch-worker",
        session_factory=migration_database,
    )
    payload = await migration_module._prepare_migration(context, claimed.payload)
    await migration_module._build_migration(context, payload)
    await migration_module._catch_up_migration(context, payload)
    validation = await migration_module._validate_migration(context, payload)

    async def _unexpected_reload(*args, **kwargs):
        raise AssertionError("final switch must not reload and re-split all knowledge sources")

    monkeypatch.setattr(
        migration_module.knowledge_base_migration_crud,
        "list_current_sources",
        _unexpected_reload,
    )
    execution = await migration_module._switch_migration(context, payload, validation)

    assert execution.finalized is True
    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        current_document = await db.get(KnowledgeBaseDocument, document.id)
    assert current is not None
    assert current.active_collection_name == "stage7-short-switch-target"
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
    _patch_migration_backend(monkeypatch, migration_module, backend)
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
            target_collection_name="stage7-managed-switch-target",
            dedupe_key="stage7-managed-switch-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-managed-switch-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="stage7-managed-switch-worker",
        session_factory=migration_database,
    )
    execution = await migration_module.handle_embedding_migration(context)
    assert execution.finalized is True

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
        migrated_first = await db.get(ManagedKnowledgeItem, first.id)
        migrated_second = await db.get(ManagedKnowledgeItem, second.id)
    assert current is not None
    assert current.active_collection_name == "stage7-managed-switch-target"
    assert current.active_embedding_model_id == "embedding-v2"
    assert current.active_embedding_dimensions == 3
    assert current.active_embedding_signature == "signature-v2"
    assert migrated_first is not None and migrated_first.indexed_version == 1
    assert migrated_second is not None and migrated_second.indexed_version == 1
    assert migrated_first.vector_item_ids != ["legacy-managed-first"]
    assert migrated_second.vector_item_ids != ["legacy-managed-second"]
    target_ids = set(backend.collections["stage7-managed-switch-target"]["items"])
    assert set(migrated_first.vector_item_ids).issubset(target_ids)
    assert set(migrated_second.vector_item_ids).issubset(target_ids)


@pytest.mark.asyncio
async def test_managed_memory_follow_submission_completes_to_final_embedding_configuration(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
    kb = await _create_container(
        migration_database,
        uid="managed-memory-follow-final-user",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
    )
    async with migration_database() as db:
        item = ManagedKnowledgeItem(
            knowledge_base_id=kb.id,
            uid=kb.uid,
            knowledge_key="managed.follow.final",
            content="managed knowledge follows the final memory embedding configuration",
            content_hash="3" * 64,
            version=1,
            indexed_version=1,
            vector_item_ids=["legacy-managed-follow-final"],
            is_recallable=True,
        )
        db.add(item)
        await db.commit()
        jobs = await submit_managed_knowledge_base_migrations_for_memory_revision(
            db,
            uid=kb.uid,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="memory-final-signature-v2",
            memory_revision=2,
        )

    assert len(jobs) == 1
    job = jobs[0]
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="managed-memory-follow-final-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="managed-memory-follow-final-worker",
        session_factory=migration_database,
    )
    execution = await migration_module.handle_embedding_migration(context)
    assert execution.finalized is True

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
    assert current is not None
    assert current.migration_status == KnowledgeBaseMigrationStatus.SUCCEEDED
    assert current.active_embedding_channel_id == kb.active_embedding_channel_id
    assert current.active_embedding_model_id == "embedding-v2"
    assert current.active_embedding_dimensions == 3
    assert current.active_embedding_signature == "memory-final-signature-v2"


@pytest.mark.asyncio
async def test_managed_memory_follow_retries_immediately_three_times_then_keeps_previous_embedding(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
    kb = await _create_container(
        migration_database,
        uid="managed-memory-follow-retry-user",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
    )
    async with migration_database() as db:
        jobs = await submit_managed_knowledge_base_migrations_for_memory_revision(
            db,
            uid=kb.uid,
            target_channel_id=kb.active_embedding_channel_id,
            target_model_id="embedding-v2",
            target_dimensions=3,
            target_signature="memory-retry-signature-v2",
            memory_revision=2,
        )

    assert len(jobs) == 1
    job = jobs[0]
    assert job.id is not None
    assert job.max_attempts == 3

    async def always_retryable(_context: KnowledgeJobExecutionContext):
        raise KnowledgeJobRetryableError("managed migration retryable failure")

    consumer = KnowledgeJobConsumer(
        KnowledgeJobExecutor(
            {KnowledgeJobOperation.EMBEDDING_MIGRATION: always_retryable},
            session_factory=migration_database,
        ),
        session_factory=migration_database,
    )

    for attempt in (1, 2, 3):
        owner = f"managed-memory-follow-retry-worker-{attempt}"
        claimed = await _claim(
            migration_database,
            uid=kb.uid,
            job_id=job.id,
            owner=owner,
        )
        assert claimed.attempt_count == attempt
        await consumer._execute(claimed, owner)

        async with migration_database() as db:
            current_job = await knowledge_job_crud.get_by_id(db, uid=kb.uid, job_id=job.id)
            current_kb = await db.get(KnowledgeBase, kb.id)
            now = await get_database_time(db)

        assert current_job is not None
        assert current_kb is not None
        assert current_kb.active_embedding_model_id == "embedding-v1"
        assert current_kb.active_embedding_signature == "signature-v1"
        if attempt < 3:
            assert current_job.status == KnowledgeJobStatus.RETRY
            assert current_job.available_at <= now
            assert current_kb.migration_status == KnowledgeBaseMigrationStatus.PREPARING
            assert current_kb.target_embedding_model_id == "embedding-v2"
        else:
            assert current_job.status == KnowledgeJobStatus.FAILED
            assert current_kb.migration_status == KnowledgeBaseMigrationStatus.FAILED
            assert current_kb.target_embedding_model_id is None
            assert current_kb.target_collection_name is None


@pytest.mark.asyncio
async def test_switch_rejects_validated_plan_when_delta_arrives_before_lock(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
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
            target_collection_name="stage7-switch-fence-target",
            dedupe_key="stage7-switch-fence-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-switch-fence-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="stage7-switch-fence-worker",
        session_factory=migration_database,
    )
    payload = await migration_module._prepare_migration(context, claimed.payload)
    await migration_module._build_migration(context, payload)
    await migration_module._catch_up_migration(context, payload)
    validation = await migration_module._validate_migration(context, payload)

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
        await migration_module._switch_migration(context, payload, validation)

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
    _patch_migration_backend(monkeypatch, migration_module, backend)
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
            target_collection_name="stage7-validation-target",
            dedupe_key="stage7-validation-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-validation-worker",
    )

    original_validate = backend.validate

    async def _invalid_validate(*args, **kwargs):
        result = await original_validate(*args, **kwargs)
        return SimpleNamespace(
            exists=result.exists,
            valid=False,
            count=result.count,
            metadata=result.metadata,
            sample_dimension=result.sample_dimension,
            errors=("forced_validation_failure",),
        )

    monkeypatch.setattr(migration_module, "async_validate_collection", _invalid_validate)
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="stage7-validation-worker",
        session_factory=migration_database,
    )
    with pytest.raises(KnowledgeJobRetryableError):
        await migration_module.handle_embedding_migration(context)

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
    assert current is not None
    assert current.active_collection_name == kb.active_collection_name
    assert current.target_collection_name == "stage7-validation-target"
    assert current.migration_status == KnowledgeBaseMigrationStatus.VALIDATING


@pytest.mark.asyncio
async def test_old_collection_cleanup_failure_is_independent_from_migration_success(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
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
            target_collection_name="stage7-cleanup-target",
            dedupe_key="stage7-cleanup-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-cleanup-migration-worker",
    )
    executor = create_default_knowledge_job_executor(session_factory=migration_database)
    migration_result = await executor.execute_claimed(
        claimed,
        "stage7-cleanup-migration-worker",
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
        owner="stage7-cleanup-worker",
    )
    cleanup_context = KnowledgeJobExecutionContext(
        job=cleanup_claim,
        worker_id="stage7-cleanup-worker",
        session_factory=migration_database,
    )
    with pytest.raises(KnowledgeJobRetryableError):
        await migration_module.handle_old_collection_cleanup(cleanup_context)

    async with migration_database() as db:
        current = await db.get(KnowledgeBase, kb.id)
    assert current is not None
    assert current.active_collection_name == "stage7-cleanup-target"
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
                target_collection_name="stage7-cleanup-next-target",
                dedupe_key="stage7-cleanup-next-migration",
            )
        await db.rollback()
        assert await knowledge_job_crud.release_for_retry(
            db,
            uid=kb.uid,
            job_id=cleanup_job.id,
            owner="stage7-cleanup-worker",
            error="retry cleanup",
            delay_seconds=0,
        )

    cleanup_retry_claim = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=cleanup_job.id,
        owner="stage7-cleanup-retry-worker",
    )
    cleanup_retry_context = KnowledgeJobExecutionContext(
        job=cleanup_retry_claim,
        worker_id="stage7-cleanup-retry-worker",
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
            target_collection_name="stage7-cleanup-next-target",
            dedupe_key="stage7-cleanup-next-migration",
        )
    assert next_job.id is not None


@pytest.mark.asyncio
async def test_worker_lease_expiry_resumes_migration_even_at_attempt_limit(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
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
            target_collection_name="stage7-restart-target",
            dedupe_key="stage7-restart-migration",
            max_attempts=1,
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-restart-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="stage7-restart-worker",
        session_factory=migration_database,
    )
    await migration_module._prepare_migration(context, claimed.payload)

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
    assert current_kb.target_collection_name == "stage7-restart-target"
    reclaimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-restart-worker-2",
    )
    assert reclaimed.attempt_count == 2


@pytest.mark.asyncio
async def test_cancelled_migration_keeps_old_collection_and_cleans_target(
    migration_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.knowledge_jobs import migration as migration_module

    backend = _VectorBackend()
    _patch_migration_backend(monkeypatch, migration_module, backend)
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
            target_collection_name="stage7-cancel-target",
            dedupe_key="stage7-cancel-migration",
        )
    assert job.id is not None
    claimed = await _claim(
        migration_database,
        uid=kb.uid,
        job_id=job.id,
        owner="stage7-cancel-worker",
    )
    context = KnowledgeJobExecutionContext(
        job=claimed,
        worker_id="stage7-cancel-worker",
        session_factory=migration_database,
    )
    await migration_module._prepare_migration(context, claimed.payload)
    assert "stage7-cancel-target" in backend.collections

    async with migration_database() as db:
        released = await knowledge_job_crud.release_for_retry(
            db,
            uid=kb.uid,
            job_id=job.id,
            owner="stage7-cancel-worker",
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
    assert current is not None
    assert current.active_collection_name == kb.active_collection_name
    assert current.migration_status == KnowledgeBaseMigrationStatus.CANCELLED
    assert current.target_collection_name is None
    assert cancelled_job is not None and cancelled_job.status == KnowledgeJobStatus.CANCELLED
    assert "stage7-cancel-target" not in backend.collections
