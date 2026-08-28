from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.knowledge_base import knowledge_base_crud
from app.core.crud.memory import memory_store_crud
from app.core.knowledge.errors import (
    ManagedKnowledgeContainerConflictError,
    ManagedKnowledgeRuntimeUnavailableError,
    ManagedKnowledgeValidationError,
)
from app.core.knowledge.managed_container import get_or_create_managed_knowledge_base
from app.core.knowledge_jobs.manager import knowledge_job_manager
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseType,
    KnowledgeJob,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeSourceType,
)
from app.models.memory import LongTermMemoryStore
from app.models.profile import Profile
from app.models.prompt import PromptLibrary

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    LongTermMemoryStore.__table__,
    KnowledgeBase.__table__,
    KnowledgeBaseCollectionOwner.__table__,
    KnowledgeBaseProfileBinding.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeJob.__table__,
)


@pytest_asyncio.fixture
async def stage5_database(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "managed-knowledge-stage5.db"
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


async def _create_profile_runtime(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    channel_active: bool = True,
    create_store: bool = True,
) -> tuple[Profile, ModelChannel]:
    async with session_factory() as db:
        channel = ModelChannel(
            name="stage5-embedding",
            api_key="test-api-key",
            base_url="https://embedding.invalid/v1",
            is_active=channel_active,
            model_ids=[
                {
                    "model_id": "embedding-model",
                    "usage": "EMBEDDING",
                    "protocol": "OPENAI_EMBEDDING",
                    "embedding_dimensions": 3,
                    "is_enabled": True,
                }
            ],
        )
        db.add(channel)
        await db.flush()

        library = PromptLibrary(name="stage5-prompts", uid="user-1", content="prompt")
        db.add(library)
        await db.flush()
        profile = Profile(name="Stage 5 Profile", uid="user-1", prompt_id=library.id, configs={})
        db.add(profile)
        await db.flush()

        if create_store:
            db.add(
                LongTermMemoryStore(
                    uid="user-1",
                    active_embedding_channel_id=channel.id,
                    active_embedding_model_id="embedding-model",
                    active_embedding_dimensions=3,
                    active_embedding_signature="embedding-signature-v7",
                    active_embedding_revision=7,
                    active_collection_name="memory-active-v7",
                    index_revision=4,
                )
            )
        await db.commit()
        await db.refresh(profile)
        await db.refresh(channel)
        return profile, channel


async def _submit_profile_create(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    profile_id: int,
    knowledge_key: str,
    content: str,
    dedupe_key: str,
):
    async with session_factory() as db:
        return await knowledge_job_manager.submit_create_for_profile(
            db,
            uid="user-1",
            profile_id=profile_id,
            knowledge_key=knowledge_key,
            content=content,
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
            dedupe_key=dedupe_key,
        )


@pytest.mark.asyncio
async def test_first_write_creates_managed_container_binding_and_first_job(
    stage5_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, channel = await _create_profile_runtime(stage5_database)

    result = await _submit_profile_create(
        stage5_database,
        profile_id=profile.id,
        knowledge_key="project.architecture",
        content="The project uses an event driven architecture.",
        dedupe_key="stage5-first-write",
    )

    assert result.knowledge_base_created is True
    assert result.knowledge_base.knowledge_base_type == KnowledgeBaseType.LLM_MANAGED
    assert result.knowledge_base.managed_profile_id == profile.id
    assert result.knowledge_base.embedding_channel_id == channel.id
    assert result.knowledge_base.embedding_model_id == "embedding-model"
    assert result.knowledge_base.embedding_dimensions == 3
    assert result.knowledge_base.active_embedding_channel_id == channel.id
    assert result.knowledge_base.active_embedding_model_id == "embedding-model"
    assert result.knowledge_base.active_embedding_dimensions == 3
    assert result.knowledge_base.active_embedding_signature == "embedding-signature-v7"
    assert result.knowledge_base.active_embedding_revision == 7
    assert result.knowledge_base.active_collection_name == result.knowledge_base.collection_name
    assert result.knowledge_base.active_collection_name != "memory-active-v7"
    assert result.job is not None and result.item is not None
    assert result.item.pending_job_id == result.job.id

    async with stage5_database() as db:
        binding = await db.scalar(
            select(KnowledgeBaseProfileBinding).where(
                KnowledgeBaseProfileBinding.knowledge_base_id == result.knowledge_base.id,
                KnowledgeBaseProfileBinding.profile_id == profile.id,
                KnowledgeBaseProfileBinding.uid == "user-1",
            )
        )
        assert binding is not None
        collection_owner = await db.get(
            KnowledgeBaseCollectionOwner,
            result.knowledge_base.collection_name,
        )
        assert collection_owner is not None
        assert collection_owner.knowledge_base_id == result.knowledge_base.id


@pytest.mark.asyncio
async def test_first_write_without_memory_runtime_leaves_no_empty_container(
    stage5_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_profile_runtime(stage5_database, create_store=False)

    with pytest.raises(ManagedKnowledgeRuntimeUnavailableError):
        await _submit_profile_create(
            stage5_database,
            profile_id=profile.id,
            knowledge_key="missing.runtime",
            content="This must not create an empty knowledge base.",
            dedupe_key="stage5-no-runtime",
        )

    async with stage5_database() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeBase)) == 0
        assert await db.scalar(select(func.count()).select_from(KnowledgeBaseProfileBinding)) == 0
        assert await db.scalar(select(func.count()).select_from(ManagedKnowledgeItem)) == 0
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_first_write_with_disabled_embedding_runtime_leaves_no_empty_container(
    stage5_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_profile_runtime(stage5_database, channel_active=False)

    with pytest.raises(ManagedKnowledgeRuntimeUnavailableError):
        await _submit_profile_create(
            stage5_database,
            profile_id=profile.id,
            knowledge_key="disabled.runtime",
            content="Disabled embedding runtime must reject lazy creation.",
            dedupe_key="stage5-disabled-runtime",
        )

    async with stage5_database() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeBase)) == 0
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_first_write_rejects_memory_runtime_change_during_lazy_create(
    stage5_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, _channel = await _create_profile_runtime(stage5_database)
    original_lock = memory_store_crud.lock_for_mutation

    async def changed_lock(*args, **kwargs):
        store = await original_lock(*args, **kwargs)
        assert store is not None
        return store.model_copy(
            update={
                "active_embedding_revision": store.active_embedding_revision + 1,
                "active_embedding_signature": "embedding-signature-v8",
            }
        )

    monkeypatch.setattr(memory_store_crud, "lock_for_mutation", changed_lock)

    with pytest.raises(ManagedKnowledgeContainerConflictError):
        await _submit_profile_create(
            stage5_database,
            profile_id=profile.id,
            knowledge_key="runtime.changed",
            content="The runtime changed while the managed container was being created.",
            dedupe_key="stage5-runtime-changed",
        )

    async with stage5_database() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeBase)) == 0
        assert await db.scalar(select(func.count()).select_from(KnowledgeBaseProfileBinding)) == 0
        assert await db.scalar(select(func.count()).select_from(ManagedKnowledgeItem)) == 0
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_managed_profile_unique_conflict_uses_current_winner(
    stage5_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, channel = await _create_profile_runtime(stage5_database)
    async with stage5_database() as db:
        winner = KnowledgeBase(
            uid="user-1",
            name="winner",
            embedding_channel_id=channel.id,
            embedding_model_id="embedding-model",
            embedding_dimensions=3,
            collection_name="managed-winner",
            knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
            managed_profile_id=profile.id,
            active_embedding_channel_id=channel.id,
            active_embedding_model_id="embedding-model",
            active_embedding_dimensions=3,
            active_embedding_signature="embedding-signature-v7",
            active_embedding_revision=7,
            active_collection_name="managed-winner",
            index_revision=1,
        )
        db.add(winner)
        await db.commit()
        await db.refresh(winner)
        winner_id = winner.id

    async def hide_existing(*_args, **_kwargs):
        return None

    lock_calls = 0

    async def current_read_winner(db: AsyncSession, **_kwargs):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 1:
            return None
        return await db.get(KnowledgeBase, winner_id)

    async def raise_unique_conflict(*_args, **_kwargs):
        raise IntegrityError(
            "INSERT INTO knowledge_base ...",
            {},
            Exception("UNIQUE constraint failed: knowledge_base.managed_profile_id"),
        )

    monkeypatch.setattr(knowledge_base_crud, "get_managed_by_profile", hide_existing)
    monkeypatch.setattr(knowledge_base_crud, "lock_managed_by_profile", current_read_winner)
    monkeypatch.setattr(knowledge_base_crud, "create", raise_unique_conflict)

    async with stage5_database() as db:
        result = await get_or_create_managed_knowledge_base(
            db,
            uid="user-1",
            profile_id=profile.id,
        )
        await db.commit()

    assert result.created is False
    assert result.knowledge_base.id == winner_id
    assert lock_calls == 2
    async with stage5_database() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeBase)) == 1
        assert await db.scalar(select(func.count()).select_from(KnowledgeBaseProfileBinding)) == 1


@pytest.mark.asyncio
async def test_first_write_failure_rolls_back_container_binding_and_job(
    stage5_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_profile_runtime(stage5_database)

    with pytest.raises(ManagedKnowledgeValidationError):
        await _submit_profile_create(
            stage5_database,
            profile_id=profile.id,
            knowledge_key="invalid.empty",
            content="   ",
            dedupe_key="stage5-invalid-first-write",
        )

    async with stage5_database() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeBase)) == 0
        assert await db.scalar(select(func.count()).select_from(KnowledgeBaseProfileBinding)) == 0
        assert await db.scalar(select(func.count()).select_from(ManagedKnowledgeItem)) == 0
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 0


@pytest.mark.asyncio
async def test_concurrent_first_writes_converge_to_one_managed_container(
    stage5_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_profile_runtime(stage5_database)
    first, second = await asyncio.gather(
        _submit_profile_create(
            stage5_database,
            profile_id=profile.id,
            knowledge_key="concurrent.one",
            content="Concurrent first item one.",
            dedupe_key="stage5-concurrent-one",
        ),
        _submit_profile_create(
            stage5_database,
            profile_id=profile.id,
            knowledge_key="concurrent.two",
            content="Concurrent first item two.",
            dedupe_key="stage5-concurrent-two",
        ),
    )

    assert first.knowledge_base.id == second.knowledge_base.id
    assert {first.knowledge_base_created, second.knowledge_base_created} == {False, True}
    async with stage5_database() as db:
        assert await db.scalar(select(func.count()).select_from(KnowledgeBase)) == 1
        assert await db.scalar(select(func.count()).select_from(KnowledgeBaseProfileBinding)) == 1
        assert await db.scalar(select(func.count()).select_from(ManagedKnowledgeItem)) == 2
        assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 2


@pytest.mark.asyncio
async def test_write_after_managed_container_delete_creates_new_identifier_and_time(
    stage5_database: async_sessionmaker[AsyncSession],
) -> None:
    profile, _channel = await _create_profile_runtime(stage5_database)
    first = await _submit_profile_create(
        stage5_database,
        profile_id=profile.id,
        knowledge_key="before.delete",
        content="Knowledge before deleting the managed container.",
        dedupe_key="stage5-before-delete",
    )
    first_id = first.knowledge_base.id
    first_created_at = first.knowledge_base.created_at

    async with stage5_database() as db:
        stored = await db.get(KnowledgeBase, first_id)
        assert stored is not None
        await db.delete(stored)
        await db.commit()

    second = await _submit_profile_create(
        stage5_database,
        profile_id=profile.id,
        knowledge_key="after.delete",
        content="Knowledge after deleting the managed container.",
        dedupe_key="stage5-after-delete",
    )

    assert second.knowledge_base_created is True
    assert second.knowledge_base.id != first_id
    assert second.knowledge_base.created_at is not None
    assert first_created_at is not None
    assert second.knowledge_base.created_at >= first_created_at

