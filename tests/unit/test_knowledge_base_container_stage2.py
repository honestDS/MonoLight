from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import app.api.v1.profile as profile_api
import app.core.knowledge_base_collection_cleanup as collection_cleanup
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseOldCollectionCleanupStatus,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseResponse,
    KnowledgeBaseType,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    KnowledgeBaseCollectionOwner.__table__,
    KnowledgeBaseProfileBinding.__table__,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=_TABLES))

    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _create_channel(session: AsyncSession, name: str = "embedding") -> ModelChannel:
    channel = ModelChannel(
        name=name,
        api_key="enc:v1:test-api-key",
        base_url="https://example.invalid",
        model_ids=[],
    )
    session.add(channel)
    await session.flush()
    return channel


async def _create_profile(session: AsyncSession, name: str = "profile") -> Profile:
    library = PromptLibrary(name=f"{name}-prompts", uid="test-user", content="test prompt")
    session.add(library)
    await session.flush()
    profile = Profile(name=name, uid="test-user", prompt_id=library.id, configs={})
    session.add(profile)
    await session.flush()
    return profile


async def _create_knowledge_base(
    session: AsyncSession,
    channel_id: int,
    *,
    name: str,
    uid: str | None = None,
    knowledge_base_type: KnowledgeBaseType = KnowledgeBaseType.USER,
    managed_profile_id: int | None = None,
    flush: bool = True,
) -> KnowledgeBase:
    knowledge_base = KnowledgeBase(
        uid=uid if uid is not None else f"uid-{name}",
        name=name,
        description=None,
        embedding_channel_id=channel_id,
        embedding_model_id="embedding-model",
        embedding_dimensions=1536,
        collection_name=f"collection-{name}",
        knowledge_base_type=knowledge_base_type,
        managed_profile_id=managed_profile_id,
    )
    session.add(knowledge_base)
    if flush:
        await session.flush()
    return knowledge_base


def _legacy_knowledge_base() -> dict[str, object]:
    return {
        "id": 1,
        "uid": "legacy-uid",
        "name": "Legacy knowledge base",
        "description": "legacy",
        "embedding_channel_id": 17,
        "embedding_model_id": "legacy-embedding-model",
        "embedding_dimensions": 1536,
        "collection_name": "legacy-collection",
    }


def _assert_empty_migration_state(response: KnowledgeBaseResponse) -> None:
    none_fields = (
        "target_embedding_channel_id target_embedding_model_id "
        "target_embedding_dimensions target_embedding_signature "
        "target_embedding_revision target_collection_name "
        "migration_job_id migration_status migration_snapshot_boundary "
        "migration_cursor migration_error migration_started_at migration_finished_at "
        "old_collection_name old_collection_cleanup_job_id "
        "old_collection_cleanup_error old_collection_cleanup_at"
    ).split()
    for field in none_fields:
        assert getattr(response, field) is None
    zero_fields = ("migration_total_count migration_success_count migration_failure_count migration_delta_high_watermark migration_delta_applied_watermark").split()
    for field in zero_fields:
        assert getattr(response, field) == 0
    assert response.old_collection_cleanup_status == (KnowledgeBaseOldCollectionCleanupStatus.NONE)


async def _assert_commit_integrity_error(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


def test_knowledge_base_response_backfills_legacy_active_state() -> None:
    legacy = _legacy_knowledge_base()

    response = KnowledgeBaseResponse.model_validate(legacy)

    for field in (
        "embedding_channel_id",
        "embedding_model_id",
        "embedding_dimensions",
        "collection_name",
    ):
        assert getattr(response, field) == legacy[field]
    assert response.knowledge_base_type == KnowledgeBaseType.USER
    assert response.managed_profile_id is None
    for field in (
        "active_embedding_channel_id",
        "active_embedding_model_id",
        "active_embedding_dimensions",
        "active_collection_name",
    ):
        legacy_field = field.removeprefix("active_")
        assert getattr(response, field) == legacy[legacy_field]
    assert response.active_embedding_signature is None
    assert response.active_embedding_revision == 1
    assert response.index_revision == 1
    assert response.index_status == KnowledgeBaseIndexStatus.READY
    _assert_empty_migration_state(response)


@pytest.mark.asyncio
async def test_knowledge_base_constraints_allow_users_and_limit_managed(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(db_session)
    first_user = await _create_knowledge_base(db_session, channel.id, name="user-one")
    second_user = await _create_knowledge_base(db_session, channel.id, name="user-two")
    await db_session.commit()
    assert first_user.managed_profile_id is None
    assert second_user.managed_profile_id is None

    profile = await _create_profile(db_session, "managed-profile")
    await _create_knowledge_base(
        db_session,
        channel.id,
        name="managed-one",
        uid=profile.uid,
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
    )
    await db_session.commit()
    await _create_knowledge_base(
        db_session,
        channel.id,
        name="managed-two",
        uid=profile.uid,
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        flush=False,
    )
    await _assert_commit_integrity_error(db_session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "collection_field",
    ("active_collection_name", "target_collection_name", "old_collection_name"),
)
async def test_knowledge_base_constraints_reject_duplicate_collection_names(
    db_session: AsyncSession,
    collection_field: str,
) -> None:
    channel = await _create_channel(db_session)
    first = await _create_knowledge_base(db_session, channel.id, name="first")
    setattr(first, collection_field, "shared-collection")
    await db_session.commit()

    second = await _create_knowledge_base(
        db_session,
        channel.id,
        name="second",
        flush=False,
    )
    setattr(second, collection_field, "shared-collection")
    await _assert_commit_integrity_error(db_session)


@pytest.mark.asyncio
async def test_knowledge_base_constraints_require_matching_owner(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(db_session)
    profile = await _create_profile(db_session)

    await _create_knowledge_base(
        db_session,
        channel.id,
        name="user-with-owner",
        uid=profile.uid,
        knowledge_base_type=KnowledgeBaseType.USER,
        managed_profile_id=profile.id,
        flush=False,
    )
    await _assert_commit_integrity_error(db_session)

    await _create_knowledge_base(
        db_session,
        channel.id,
        name="managed-without-owner",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        flush=False,
    )
    await _assert_commit_integrity_error(db_session)


@pytest.mark.asyncio
async def test_knowledge_base_constraints_reject_cross_user_managed_profile(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(db_session)
    profile = await _create_profile(db_session)

    await _create_knowledge_base(
        db_session,
        channel.id,
        name="cross-user-managed",
        uid="other-user",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        flush=False,
    )
    await _assert_commit_integrity_error(db_session)


@pytest.mark.asyncio
async def test_knowledge_base_profile_binding_requires_matching_owner(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(db_session)
    profile = await _create_profile(db_session)
    same_user = await _create_knowledge_base(
        db_session,
        channel.id,
        name="same-user",
        uid=profile.uid,
    )
    other_user = await _create_knowledge_base(
        db_session,
        channel.id,
        name="other-user",
        uid="other-user",
    )

    db_session.add(
        KnowledgeBaseProfileBinding(
            uid=profile.uid,
            profile_id=profile.id,
            knowledge_base_id=same_user.id,
        )
    )
    await db_session.commit()

    db_session.add(
        KnowledgeBaseProfileBinding(
            uid=profile.uid,
            profile_id=profile.id,
            knowledge_base_id=other_user.id,
        )
    )
    await _assert_commit_integrity_error(db_session)


@pytest.mark.asyncio
async def test_profile_delete_cascades_managed_knowledge_base(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = await _create_channel(db_session)
    profile = await _create_profile(db_session)
    other_profile = await _create_profile(db_session, "other-profile")
    managed = await _create_knowledge_base(
        db_session,
        channel.id,
        name="managed",
        uid=profile.uid,
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
    )
    user = await _create_knowledge_base(db_session, channel.id, name="user", uid=profile.uid)
    managed.collection_name = "managed-collection"
    managed.active_collection_name = "managed-active-collection"
    managed.target_collection_name = "managed-target-collection"
    managed.old_collection_name = "managed-old-collection"
    db_session.add(
        KnowledgeBaseProfileBinding(
            uid=profile.uid,
            profile_id=profile.id,
            knowledge_base_id=user.id,
        )
    )
    await db_session.commit()

    assert await db_session.get(Profile, profile.id) is not None
    assert await db_session.get(KnowledgeBase, managed.id) is not None
    assert await db_session.get(KnowledgeBase, user.id) is not None
    bindings = (await db_session.execute(select(KnowledgeBaseProfileBinding))).scalars().all()
    assert [(binding.uid, binding.profile_id, binding.knowledge_base_id) for binding in bindings] == [(profile.uid, profile.id, user.id)]

    permission_checks = 0

    async def _false_async(*_args: object, **_kwargs: object) -> bool:
        nonlocal permission_checks
        permission_checks += 1
        return False

    deleted_collections: list[str] = []
    target_failure_pending = True

    async def _delete_collection_if_exists(collection_name: str) -> bool:
        nonlocal target_failure_pending
        deleted_collections.append(collection_name)
        if collection_name == managed.target_collection_name and target_failure_pending:
            target_failure_pending = False
            raise RuntimeError("temporary collection delete failure")
        return collection_name == managed.active_collection_name

    monkeypatch.setattr(profile_api.session_crud, "has_profile_override", _false_async)
    monkeypatch.setattr(profile_api.message_platform_crud, "has_profile_assignment", _false_async)
    monkeypatch.setattr(profile_api.scheduled_task_crud, "has_profile_assignment", _false_async)
    monkeypatch.setattr(
        collection_cleanup,
        "async_delete_collection_if_exists",
        _delete_collection_if_exists,
    )

    await profile_api.delete_profile(
        profile_id=profile.id,
        db=db_session,
        current_user=SimpleNamespace(uid="test-user", is_superuser=False),
    )

    assert deleted_collections == []
    assert permission_checks == 3
    remaining = (await db_session.execute(select(KnowledgeBase))).scalars().all()
    assert [knowledge_base.id for knowledge_base in remaining] == [user.id]
    assert managed.id not in [knowledge_base.id for knowledge_base in remaining]
    remaining_profiles = (await db_session.execute(select(Profile))).scalars().all()
    assert [remaining_profile.id for remaining_profile in remaining_profiles] == [other_profile.id]
    bindings = (await db_session.execute(select(KnowledgeBaseProfileBinding))).scalars().all()
    assert bindings == []

    expected_collections = {
        managed.collection_name,
        managed.active_collection_name,
        managed.target_collection_name,
        managed.old_collection_name,
    }
    assert len(expected_collections) == 4
    owners = (await db_session.execute(select(KnowledgeBaseCollectionOwner).order_by(KnowledgeBaseCollectionOwner.collection_name))).scalars().all()
    assert {owner.collection_name for owner in owners} == expected_collections
    assert len(owners) == 4
    assert all(owner.knowledge_base_id is None for owner in owners)
    assert user.collection_name not in {owner.collection_name for owner in owners}

    first_result = await collection_cleanup.process_pending_collection_cleanups(db_session)
    assert first_result.pending_count == 4
    assert first_result.succeeded_count == 3
    assert first_result.failed_count == 1
    assert set(deleted_collections) == expected_collections

    remaining_owners = (await db_session.execute(select(KnowledgeBaseCollectionOwner))).scalars().all()
    assert [owner.collection_name for owner in remaining_owners] == [managed.target_collection_name]
    failed_owner = remaining_owners[0]
    assert failed_owner.knowledge_base_id is None
    assert failed_owner.cleanup_attempt_count == 1
    assert failed_owner.cleanup_error == "RuntimeError: temporary collection delete failure"
    remaining_profile_ids = (await db_session.execute(select(Profile.id).order_by(Profile.id))).scalars().all()
    remaining_knowledge_base_ids = (await db_session.execute(select(KnowledgeBase.id).order_by(KnowledgeBase.id))).scalars().all()
    assert remaining_profile_ids == [other_profile.id]
    assert remaining_knowledge_base_ids == [user.id]

    second_result = await collection_cleanup.process_pending_collection_cleanups(db_session)
    assert second_result.pending_count == 1
    assert second_result.succeeded_count == 1
    assert second_result.failed_count == 0
    assert deleted_collections[-1] == managed.target_collection_name
    assert (await db_session.execute(select(KnowledgeBaseCollectionOwner))).scalars().all() == []
