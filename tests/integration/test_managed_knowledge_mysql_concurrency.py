from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.core.knowledge.managed_container as managed_container
from app.core.crud.knowledge.managed import managed_knowledge_item_crud
from app.core.crud.memory.store import memory_store_crud
from app.core.knowledge.embedding_migration import submit_managed_knowledge_base_migrations_for_memory_revision
from app.core.knowledge.managed import managed_knowledge_service
from app.core.knowledge.managed_container import get_or_create_managed_knowledge_base
from app.core.knowledge.results import ManagedKnowledgeMutationStatus
from app.core.knowledge_jobs.manager import knowledge_job_manager
from app.core.knowledge_jobs.migration import prepare_knowledge_base_embedding_migration
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseMigrationStatus,
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

_MYSQL_TEST_URL_ENV = "MONOLIGH_TEST_MYSQL_URL"
_MYSQL_TEST_ALLOWED_DATABASE_ENV = "MONOLIGH_TEST_MYSQL_ALLOWED_DATABASE"
_MYSQL_TABLES = (
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


def _mysql_test_url() -> str:
    raw_url = os.getenv(_MYSQL_TEST_URL_ENV)
    if not raw_url:
        pytest.skip(f"{_MYSQL_TEST_URL_ENV} is not configured")

    url = make_url(raw_url)
    if url.get_backend_name() != "mysql":
        pytest.fail(f"{_MYSQL_TEST_URL_ENV} must use a MySQL URL")
    if url.get_driver_name() not in {"aiomysql", "asyncmy"}:
        pytest.fail(f"{_MYSQL_TEST_URL_ENV} must use an async MySQL driver")
    allowed_database = os.getenv(_MYSQL_TEST_ALLOWED_DATABASE_ENV, "").strip()
    if not url.database or ("test" not in url.database.lower() and allowed_database != url.database):
        pytest.fail(f"{_MYSQL_TEST_URL_ENV} must point to a database whose name contains 'test', or {_MYSQL_TEST_ALLOWED_DATABASE_ENV} must exactly match the database name")
    return raw_url


@pytest.mark.asyncio
async def test_managed_knowledge_duplicate_key_converges_under_mysql_repeatable_read(
    monkeypatch: pytest.MonkeyPatch,
):
    engine = create_async_engine(
        _mysql_test_url(),
        isolation_level="REPEATABLE READ",
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    suffix = uuid.uuid4().hex[:12]
    uid = f"mysql-test-{suffix}"
    collection_name = f"managed-knowledge-{suffix}"
    knowledge_base_id: int | None = None
    profile_id: int | None = None
    prompt_id: int | None = None
    channel_id: int | None = None
    schema_ready = False

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: SQLModel.metadata.create_all(
                    sync_connection,
                    tables=_MYSQL_TABLES,
                )
            )
        schema_ready = True

        async with session_factory() as setup_session:
            channel = ModelChannel(
                name=f"managed-knowledge-{suffix}",
                api_key="enc:v1:test-api-key",
                base_url="https://example.invalid",
                model_ids=[],
            )
            setup_session.add(channel)
            await setup_session.flush()
            channel_id = channel.id

            prompt = PromptLibrary(
                name=f"managed-knowledge-{suffix}",
                uid=uid,
                content="prompt",
            )
            setup_session.add(prompt)
            await setup_session.flush()
            prompt_id = prompt.id

            profile = Profile(
                name=f"managed-knowledge-{suffix}",
                uid=uid,
                prompt_id=prompt.id,
                configs={},
            )
            setup_session.add(profile)
            await setup_session.flush()
            profile_id = profile.id

            knowledge_base = KnowledgeBase(
                uid=uid,
                name=f"managed-knowledge-{suffix}",
                embedding_channel_id=channel.id,
                embedding_model_id="embedding-model",
                embedding_dimensions=1536,
                collection_name=collection_name,
                knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
                managed_profile_id=profile.id,
            )
            setup_session.add(knowledge_base)
            await setup_session.commit()
            await setup_session.refresh(knowledge_base)
            knowledge_base_id = knowledge_base.id

        original_create = managed_knowledge_item_crud.create
        reached_create = 0
        reached_lock = asyncio.Lock()
        release_creates = asyncio.Event()

        async def _synchronized_create(*args, **kwargs):
            nonlocal reached_create
            async with reached_lock:
                reached_create += 1
                if reached_create == 2:
                    release_creates.set()
            await asyncio.wait_for(release_creates.wait(), timeout=10)
            return await original_create(*args, **kwargs)

        monkeypatch.setattr(managed_knowledge_item_crud, "create", _synchronized_create)

        async def _create(content: str) -> ManagedKnowledgeMutationStatus:
            async with session_factory() as session:
                connection = await session.connection()
                isolation_level = await connection.get_isolation_level()
                assert isolation_level.upper().replace("_", " ") == "REPEATABLE READ"

                result = await managed_knowledge_service.create(
                    session,
                    uid=uid,
                    knowledge_base_id=knowledge_base_id,
                    knowledge_key="same-key",
                    content=content,
                    source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                    actor=ManagedKnowledgeActorType.LLM,
                )
                return result.status

        statuses = await asyncio.wait_for(
            asyncio.gather(
                _create("first concurrent MySQL content"),
                _create("second concurrent MySQL content"),
            ),
            timeout=30,
        )

        assert sorted(status.value for status in statuses) == sorted(
            [
                ManagedKnowledgeMutationStatus.CREATED.value,
                ManagedKnowledgeMutationStatus.EXISTING_KEY.value,
            ]
        )
    finally:
        if schema_ready:
            async with session_factory() as cleanup_session:
                if knowledge_base_id is not None:
                    await cleanup_session.execute(delete(ManagedKnowledgeRevision).where(ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id))
                    await cleanup_session.execute(delete(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id))
                    await cleanup_session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == knowledge_base_id))
                await cleanup_session.execute(delete(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.collection_name == collection_name))
                if profile_id is not None:
                    await cleanup_session.execute(delete(Profile).where(Profile.id == profile_id))
                if prompt_id is not None:
                    await cleanup_session.execute(delete(PromptLibrary).where(PromptLibrary.id == prompt_id))
                if channel_id is not None:
                    await cleanup_session.execute(delete(ModelChannel).where(ModelChannel.id == channel_id))
                await cleanup_session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_managed_knowledge_first_writes_converge_to_one_container_under_mysql_repeatable_read():
    engine = create_async_engine(
        _mysql_test_url(),
        isolation_level="REPEATABLE READ",
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    suffix = uuid.uuid4().hex[:12]
    uid = f"mysql-stage5-{suffix}"
    profile_id: int | None = None
    prompt_id: int | None = None
    channel_id: int | None = None
    collection_names: list[str] = []
    schema_ready = False

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: SQLModel.metadata.create_all(
                    sync_connection,
                    tables=_MYSQL_TABLES,
                )
            )
        schema_ready = True

        async with session_factory() as setup_session:
            channel = ModelChannel(
                name=f"managed-stage5-{suffix}",
                api_key="test-api-key",
                base_url="https://example.invalid",
                model_ids=[
                    {
                        "model_id": "embedding-model",
                        "usage": "EMBEDDING",
                        "protocol": "OPENAI_EMBEDDING",
                        "embedding_dimensions": 1536,
                        "is_enabled": True,
                    }
                ],
            )
            setup_session.add(channel)
            await setup_session.flush()
            channel_id = channel.id

            prompt = PromptLibrary(
                name=f"managed-stage5-{suffix}",
                uid=uid,
                content="prompt",
            )
            setup_session.add(prompt)
            await setup_session.flush()
            prompt_id = prompt.id

            profile = Profile(
                name=f"managed-stage5-{suffix}",
                uid=uid,
                prompt_id=prompt.id,
                configs={},
            )
            setup_session.add(profile)
            await setup_session.flush()
            profile_id = profile.id

            setup_session.add(
                LongTermMemoryStore(
                    uid=uid,
                    active_embedding_channel_id=channel.id,
                    active_embedding_model_id="embedding-model",
                    active_embedding_dimensions=1536,
                    active_embedding_signature=f"signature-{suffix}",
                    active_embedding_revision=1,
                    active_collection_name=f"memory-{suffix}",
                    index_revision=1,
                )
            )
            await setup_session.commit()

        async def _first_write(key: str, content: str, dedupe_key: str):
            async with session_factory() as session:
                connection = await session.connection()
                isolation_level = await connection.get_isolation_level()
                assert isolation_level.upper().replace("_", " ") == "REPEATABLE READ"
                return await knowledge_job_manager.submit_create_for_profile(
                    session,
                    uid=uid,
                    profile_id=profile_id,
                    knowledge_key=key,
                    content=content,
                    source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                    actor=ManagedKnowledgeActorType.LLM,
                    dedupe_key=dedupe_key,
                )

        first, second = await asyncio.wait_for(
            asyncio.gather(
                _first_write("first.key", "first MySQL lazy-create content", f"first-{suffix}"),
                _first_write("second.key", "second MySQL lazy-create content", f"second-{suffix}"),
            ),
            timeout=30,
        )

        assert first.knowledge_base.id == second.knowledge_base.id
        assert {first.knowledge_base_created, second.knowledge_base_created} == {False, True}

        async with session_factory() as verify_session:
            bases = list(
                (
                    await verify_session.execute(
                        select(KnowledgeBase).where(
                            KnowledgeBase.uid == uid,
                            KnowledgeBase.managed_profile_id == profile_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(bases) == 1
            collection_names.extend(
                name
                for name in (
                    bases[0].collection_name,
                    bases[0].active_collection_name,
                    bases[0].target_collection_name,
                    bases[0].old_collection_name,
                )
                if name
            )
            knowledge_base_id = bases[0].id
            assert knowledge_base_id is not None
            assert (
                await verify_session.scalar(
                    select(func.count())
                    .select_from(KnowledgeBaseProfileBinding)
                    .where(
                        KnowledgeBaseProfileBinding.knowledge_base_id == knowledge_base_id,
                        KnowledgeBaseProfileBinding.profile_id == profile_id,
                    )
                )
                == 1
            )
            assert await verify_session.scalar(select(func.count()).select_from(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id)) == 2
            assert await verify_session.scalar(select(func.count()).select_from(KnowledgeJob).where(KnowledgeJob.knowledge_base_id == knowledge_base_id)) == 2
    finally:
        if schema_ready:
            async with session_factory() as cleanup_session:
                bases = list((await cleanup_session.execute(select(KnowledgeBase).where(KnowledgeBase.uid == uid))).scalars().all())
                for base in bases:
                    collection_names.extend(
                        name
                        for name in (
                            base.collection_name,
                            base.active_collection_name,
                            base.target_collection_name,
                            base.old_collection_name,
                        )
                        if name
                    )
                await cleanup_session.execute(delete(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid))
                await cleanup_session.execute(delete(KnowledgeBase).where(KnowledgeBase.uid == uid))
                if collection_names:
                    await cleanup_session.execute(delete(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.collection_name.in_(set(collection_names))))
                if profile_id is not None:
                    await cleanup_session.execute(delete(Profile).where(Profile.id == profile_id))
                if prompt_id is not None:
                    await cleanup_session.execute(delete(PromptLibrary).where(PromptLibrary.id == prompt_id))
                if channel_id is not None:
                    await cleanup_session.execute(delete(ModelChannel).where(ModelChannel.id == channel_id))
                await cleanup_session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_memory_switch_sees_managed_container_created_after_repeatable_read_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        _mysql_test_url(),
        isolation_level="REPEATABLE READ",
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    suffix = uuid.uuid4().hex[:12]
    uid = f"mysql-stage8-switch-{suffix}"
    profile_id: int | None = None
    prompt_id: int | None = None
    channel_id: int | None = None
    schema_ready = False

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: SQLModel.metadata.create_all(
                    sync_connection,
                    tables=_MYSQL_TABLES,
                )
            )
        schema_ready = True

        async with session_factory() as setup_session:
            channel = ModelChannel(
                name=f"managed-stage8-switch-{suffix}",
                api_key="test-api-key",
                base_url="https://example.invalid",
                model_ids=[
                    {
                        "model_id": "embedding-v1",
                        "usage": "EMBEDDING",
                        "protocol": "OPENAI_EMBEDDING",
                        "embedding_dimensions": 1536,
                        "is_enabled": True,
                    },
                    {
                        "model_id": "embedding-v2",
                        "usage": "EMBEDDING",
                        "protocol": "OPENAI_EMBEDDING",
                        "embedding_dimensions": 3072,
                        "is_enabled": True,
                    },
                ],
            )
            setup_session.add(channel)
            await setup_session.flush()
            channel_id = channel.id

            prompt = PromptLibrary(
                name=f"managed-stage8-switch-{suffix}",
                uid=uid,
                content="prompt",
            )
            setup_session.add(prompt)
            await setup_session.flush()
            prompt_id = prompt.id

            profile = Profile(
                name=f"managed-stage8-switch-{suffix}",
                uid=uid,
                prompt_id=prompt.id,
                configs={},
            )
            setup_session.add(profile)
            await setup_session.flush()
            profile_id = profile.id

            setup_session.add(
                LongTermMemoryStore(
                    uid=uid,
                    active_embedding_channel_id=channel.id,
                    active_embedding_model_id="embedding-v1",
                    active_embedding_dimensions=1536,
                    active_embedding_signature=f"signature-v1-{suffix}",
                    active_embedding_revision=1,
                    active_collection_name=f"memory-stage8-switch-{suffix}",
                    index_revision=1,
                )
            )
            await setup_session.commit()

        async def fake_load_runtime_config(*_args, **_kwargs):
            return SimpleNamespace(declared_dimensions=1536)

        monkeypatch.setattr(
            managed_container,
            "load_embedding_runtime_config",
            fake_load_runtime_config,
        )

        async with session_factory() as switch_session:
            connection = await switch_session.connection()
            isolation_level = await connection.get_isolation_level()
            assert isolation_level.upper().replace("_", " ") == "REPEATABLE READ"

            snapshot = await memory_store_crud.get_snapshot_by_uid(switch_session, uid=uid)
            assert snapshot is not None
            assert snapshot.active_embedding_revision == 1

            async with session_factory() as create_session:
                created = await get_or_create_managed_knowledge_base(
                    create_session,
                    uid=uid,
                    profile_id=profile_id,
                )
                knowledge_base_id = created.knowledge_base.id
                assert knowledge_base_id is not None
                await create_session.commit()

            store = await memory_store_crud.lock_for_mutation(
                switch_session,
                uid=uid,
                commit=False,
            )
            assert store is not None
            store.active_embedding_model_id = "embedding-v2"
            store.active_embedding_dimensions = 3072
            store.active_embedding_signature = f"signature-v2-{suffix}"
            store.active_embedding_revision = 2
            switch_session.add(store)
            await switch_session.flush()

            jobs = await submit_managed_knowledge_base_migrations_for_memory_revision(
                switch_session,
                uid=uid,
                target_channel_id=channel_id,
                target_model_id="embedding-v2",
                target_dimensions=3072,
                target_signature=f"signature-v2-{suffix}",
                memory_revision=2,
                commit=False,
            )
            assert len(jobs) == 1
            assert jobs[0].knowledge_base_id == knowledge_base_id
            await switch_session.commit()

        async with session_factory() as verify_session:
            current = await verify_session.get(KnowledgeBase, knowledge_base_id)
            assert current is not None
            assert current.active_embedding_revision == 1
            assert current.target_embedding_model_id == "embedding-v2"
            assert current.target_embedding_dimensions == 3072
            assert current.migration_job_id == jobs[0].id
    finally:
        if schema_ready:
            async with session_factory() as cleanup_session:
                await cleanup_session.execute(delete(KnowledgeJob).where(KnowledgeJob.uid == uid))
                await cleanup_session.execute(delete(LongTermMemoryStore).where(LongTermMemoryStore.uid == uid))
                await cleanup_session.execute(delete(KnowledgeBase).where(KnowledgeBase.uid == uid))
                if profile_id is not None:
                    await cleanup_session.execute(delete(Profile).where(Profile.id == profile_id))
                if prompt_id is not None:
                    await cleanup_session.execute(delete(PromptLibrary).where(PromptLibrary.id == prompt_id))
                if channel_id is not None:
                    await cleanup_session.execute(delete(ModelChannel).where(ModelChannel.id == channel_id))
                await cleanup_session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_same_user_knowledge_base_concurrent_migration_submissions_reuse_one_job() -> None:
    engine = create_async_engine(
        _mysql_test_url(),
        isolation_level="REPEATABLE READ",
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    suffix = uuid.uuid4().hex[:12]
    uid = f"mysql-stage8-user-submit-{suffix}"
    channel_id: int | None = None
    knowledge_base_id: int | None = None
    schema_ready = False

    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: SQLModel.metadata.create_all(
                    sync_connection,
                    tables=_MYSQL_TABLES,
                )
            )
        schema_ready = True

        async with session_factory() as setup_session:
            channel = ModelChannel(
                name=f"user-stage8-submit-{suffix}",
                api_key="test-api-key",
                base_url="https://example.invalid",
                model_ids=[
                    {
                        "model_id": "embedding-v1",
                        "usage": "EMBEDDING",
                        "protocol": "OPENAI_EMBEDDING",
                        "embedding_dimensions": 1536,
                        "is_enabled": True,
                    },
                    {
                        "model_id": "embedding-v2",
                        "usage": "EMBEDDING",
                        "protocol": "OPENAI_EMBEDDING",
                        "embedding_dimensions": 3072,
                        "is_enabled": True,
                    },
                ],
            )
            setup_session.add(channel)
            await setup_session.flush()
            channel_id = channel.id

            knowledge_base = KnowledgeBase(
                uid=uid,
                name=f"user-stage8-submit-{suffix}",
                embedding_channel_id=channel.id,
                embedding_model_id="embedding-v1",
                embedding_dimensions=1536,
                collection_name=f"user-stage8-submit-{suffix}",
                knowledge_base_type=KnowledgeBaseType.USER,
                active_embedding_channel_id=channel.id,
                active_embedding_model_id="embedding-v1",
                active_embedding_dimensions=1536,
                active_embedding_signature=f"signature-v1-{suffix}",
                active_embedding_revision=1,
                active_collection_name=f"user-stage8-submit-{suffix}",
                index_revision=1,
                index_status=KnowledgeBaseIndexStatus.READY,
            )
            setup_session.add(knowledge_base)
            await setup_session.commit()
            await setup_session.refresh(knowledge_base)
            knowledge_base_id = knowledge_base.id
            assert knowledge_base_id is not None

        dedupe_key = f"mysql-stage8-user-submit-{suffix}"

        async def submit():
            async with session_factory() as session:
                return await prepare_knowledge_base_embedding_migration(
                    session,
                    uid=uid,
                    knowledge_base_id=knowledge_base_id,
                    target_channel_id=channel_id,
                    target_model_id="embedding-v2",
                    target_dimensions=3072,
                    target_signature=f"signature-v2-{suffix}",
                    dedupe_key=dedupe_key,
                )

        first, second = await asyncio.wait_for(
            asyncio.gather(submit(), submit()),
            timeout=30,
        )

        assert first.id is not None
        assert second.id == first.id

        async with session_factory() as verify_session:
            current = await verify_session.get(KnowledgeBase, knowledge_base_id)
            assert current is not None
            assert current.migration_status == KnowledgeBaseMigrationStatus.PREPARING
            assert current.migration_job_id == first.id
            assert (
                await verify_session.scalar(
                    select(func.count())
                    .select_from(KnowledgeJob)
                    .where(
                        KnowledgeJob.uid == uid,
                        KnowledgeJob.knowledge_base_id == knowledge_base_id,
                    )
                )
                == 1
            )
    finally:
        if schema_ready:
            async with session_factory() as cleanup_session:
                await cleanup_session.execute(delete(KnowledgeJob).where(KnowledgeJob.uid == uid))
                await cleanup_session.execute(delete(KnowledgeBase).where(KnowledgeBase.uid == uid))
                if channel_id is not None:
                    await cleanup_session.execute(delete(ModelChannel).where(ModelChannel.id == channel_id))
                await cleanup_session.commit()
        await engine.dispose()
