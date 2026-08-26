from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from app.core.constants import MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS
from app.core.crud.managed_knowledge import managed_knowledge_item_crud
from app.core.knowledge.managed import (
    ManagedKnowledgeConflictError,
    ManagedKnowledgeContentTooLongError,
    ManagedKnowledgeNotFoundError,
    managed_knowledge_service,
)
from app.core.knowledge.results import ManagedKnowledgeMutationStatus
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseType,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeSourceType,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
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

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _create_container(session: AsyncSession, *, managed: bool = True, name: str = "managed") -> KnowledgeBase:
    channel = ModelChannel(
        name=f"{name}-embedding",
        api_key="enc:v1:test-api-key",
        base_url="https://example.invalid",
        model_ids=[],
    )
    session.add(channel)
    await session.flush()

    library = PromptLibrary(name=f"{name}-prompts", uid="user-1", content="prompt")
    session.add(library)
    await session.flush()

    profile = Profile(name=f"{name}-profile", uid="user-1", prompt_id=library.id, configs={})
    session.add(profile)
    await session.flush()

    knowledge_base = KnowledgeBase(
        uid="user-1",
        name=name,
        embedding_channel_id=channel.id,
        embedding_model_id="embedding-model",
        embedding_dimensions=1536,
        collection_name=f"collection-{name}",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED if managed else KnowledgeBaseType.USER,
        managed_profile_id=profile.id if managed else None,
    )
    session.add(knowledge_base)
    await session.commit()
    await session.refresh(knowledge_base)
    return knowledge_base


@pytest.mark.asyncio
async def test_managed_knowledge_create_allocates_monotonic_ids_and_returns_duplicate_states(db_session: AsyncSession):
    knowledge_base = await _create_container(db_session)

    first = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_key="project.architecture",
        content="The project uses an event driven architecture.",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )
    second = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_key="project.deploy",
        content="Deployments are performed from the release branch.",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )

    assert first.status == ManagedKnowledgeMutationStatus.CREATED
    assert second.status == ManagedKnowledgeMutationStatus.CREATED
    assert first.item is not None and second.item is not None
    assert second.item.id > first.item.id
    assert first.item.version == 1
    assert first.item.indexed_version == 0
    assert first.item.vector_item_ids == []

    duplicate_key = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_key="project.architecture",
        content="Different content for the same stable key.",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )
    duplicate_content = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_key="project.architecture.copy",
        content="The project uses an event driven architecture.",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )

    assert duplicate_key.status == ManagedKnowledgeMutationStatus.EXISTING_KEY
    assert duplicate_key.item.id == first.item.id
    assert duplicate_content.status == ManagedKnowledgeMutationStatus.EXISTING_CONTENT
    assert duplicate_content.item.id == first.item.id


@pytest.mark.asyncio
async def test_user_edit_creates_revision_and_stale_llm_update_cannot_overwrite_it(db_session: AsyncSession):
    knowledge_base = await _create_container(db_session)
    created = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_key="project.rule",
        content="Initial rule",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )
    knowledge_id = created.item.id

    edited = await managed_knowledge_service.update(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_id=knowledge_id,
        expected_version=1,
        knowledge_key="project.rule",
        content="User corrected rule",
        source_type=ManagedKnowledgeSourceType.USER_API,
        actor=ManagedKnowledgeActorType.USER,
    )

    assert edited.status == ManagedKnowledgeMutationStatus.UPDATED
    assert edited.item.version == 2
    assert edited.item.last_modified_by == ManagedKnowledgeActorType.USER

    revisions = list((await db_session.execute(select(ManagedKnowledgeRevision).where(ManagedKnowledgeRevision.knowledge_id == knowledge_id).order_by(ManagedKnowledgeRevision.version.asc()))).scalars().all())
    assert [revision.version for revision in revisions] == [1, 2]
    assert revisions[0].before_snapshot is None
    assert revisions[0].after_snapshot["content"] == "Initial rule"
    assert revisions[1].before_snapshot["content"] == "Initial rule"
    assert revisions[1].after_snapshot["content"] == "User corrected rule"
    assert revisions[1].modified_by == ManagedKnowledgeActorType.USER

    with pytest.raises(ManagedKnowledgeConflictError) as exc_info:
        await managed_knowledge_service.update(
            db_session,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_id=knowledge_id,
            expected_version=1,
            knowledge_key="project.rule",
            content="Stale LLM overwrite",
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
        )
    assert exc_info.value.code == 409

    current = await db_session.get(ManagedKnowledgeItem, knowledge_id)
    assert current.version == 2
    assert current.content == "User corrected rule"


@pytest.mark.asyncio
async def test_llm_cannot_mutate_user_knowledge_base_or_locked_managed_item(db_session: AsyncSession):
    user_knowledge_base = await _create_container(db_session, managed=False, name="user-kb")

    with pytest.raises(ManagedKnowledgeConflictError):
        await managed_knowledge_service.create(
            db_session,
            uid="user-1",
            knowledge_base_id=user_knowledge_base.id,
            knowledge_key="forbidden",
            content="User-owned document content",
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
        )

    managed_knowledge_base = await _create_container(db_session, managed=True, name="managed-kb")
    managed_knowledge_base_id = managed_knowledge_base.id
    created = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=managed_knowledge_base_id,
        knowledge_key="locked",
        content="Managed knowledge initially created by user",
        source_type=ManagedKnowledgeSourceType.USER_API,
        actor=ManagedKnowledgeActorType.USER,
    )
    assert created.item.llm_maintainable is False
    locked_knowledge_id = created.item.id

    with pytest.raises(ManagedKnowledgeConflictError):
        await managed_knowledge_service.update(
            db_session,
            uid="user-1",
            knowledge_base_id=managed_knowledge_base_id,
            knowledge_id=locked_knowledge_id,
            expected_version=1,
            knowledge_key="locked",
            content="LLM should not overwrite",
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
        )

    with pytest.raises(ManagedKnowledgeConflictError):
        await managed_knowledge_service.delete(
            db_session,
            uid="user-1",
            knowledge_base_id=managed_knowledge_base_id,
            knowledge_id=locked_knowledge_id,
            expected_version=1,
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
        )


@pytest.mark.asyncio
async def test_managed_knowledge_delete_requires_expected_version_and_preserves_history(db_session: AsyncSession):
    knowledge_base = await _create_container(db_session)
    knowledge_base_id = knowledge_base.id
    created = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        knowledge_key="delete-me",
        content="Content to delete",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )
    delete_knowledge_id = created.item.id

    with pytest.raises(ManagedKnowledgeConflictError):
        await managed_knowledge_service.delete(
            db_session,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
            knowledge_id=delete_knowledge_id,
            expected_version=2,
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
        )

    deleted = await managed_knowledge_service.delete(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        knowledge_id=delete_knowledge_id,
        expected_version=1,
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )
    assert deleted.status == ManagedKnowledgeMutationStatus.DELETED
    assert deleted.item.version == 2
    assert deleted.item.deleted_at is not None
    assert deleted.item.is_recallable is False

    revisions = list((await db_session.execute(select(ManagedKnowledgeRevision).where(ManagedKnowledgeRevision.knowledge_id == delete_knowledge_id).order_by(ManagedKnowledgeRevision.version.asc()))).scalars().all())
    assert [revision.version for revision in revisions] == [1, 2]
    assert revisions[-1].before_snapshot["deleted_at"] is None
    assert revisions[-1].after_snapshot["deleted_at"] is not None


@pytest.mark.asyncio
async def test_managed_knowledge_content_limit_is_independent_and_retryable(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    knowledge_base = await _create_container(db_session)
    monkeypatch.setattr("app.core.knowledge.managed.estimate_tokens", lambda _content: MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS + 1)

    with pytest.raises(ManagedKnowledgeContentTooLongError) as exc_info:
        await managed_knowledge_service.create(
            db_session,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            knowledge_key="too-long",
            content="long content",
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
        )

    assert MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS > 160
    assert exc_info.value.data == {
        "status": "content_too_long",
        "actual_tokens": MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS + 1,
        "max_tokens": MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS,
        "retryable": True,
    }


@pytest.mark.asyncio
async def test_managed_knowledge_has_no_character_count_limit(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    knowledge_base = await _create_container(db_session, name="long-text")
    content = "x" * 120_000
    monkeypatch.setattr("app.core.knowledge.managed.estimate_tokens", lambda _content: 1000)

    result = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_key="long-text",
        content=content,
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )

    assert result.status == ManagedKnowledgeMutationStatus.CREATED
    assert result.item.content == content
    assert len(result.item.content) == 120_000
    assert result.item.content_token_count == 1000


@pytest.mark.asyncio
async def test_concurrent_managed_knowledge_creates_allocate_unique_monotonic_ids(tmp_path):
    database_path = tmp_path / "managed-knowledge-concurrency.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=_TABLES))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as setup_session:
        knowledge_base = await _create_container(setup_session, name="concurrent")
        knowledge_base_id = knowledge_base.id

    async def _create(index: int) -> int:
        async with session_factory() as session:
            result = await managed_knowledge_service.create(
                session,
                uid="user-1",
                knowledge_base_id=knowledge_base_id,
                knowledge_key=f"concurrent.{index}",
                content=f"Concurrent managed knowledge {index}",
                source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                actor=ManagedKnowledgeActorType.LLM,
            )
            assert result.status == ManagedKnowledgeMutationStatus.CREATED
            assert result.item is not None and result.item.id is not None
            return result.item.id

    try:
        knowledge_ids = await asyncio.gather(*(_create(index) for index in range(8)))
    finally:
        await engine.dispose()

    assert len(set(knowledge_ids)) == 8
    assert all(knowledge_id > 0 for knowledge_id in knowledge_ids)


@pytest.mark.asyncio
async def test_concurrent_updates_with_same_expected_version_only_publish_once(tmp_path, monkeypatch: pytest.MonkeyPatch):
    database_path = tmp_path / "managed-knowledge-version-race.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=_TABLES))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as setup_session:
        knowledge_base = await _create_container(setup_session, name="version-race")
        knowledge_base_id = knowledge_base.id
        created = await managed_knowledge_service.create(
            setup_session,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
            knowledge_key="race",
            content="version one",
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
        )
        knowledge_id = created.item.id

    original_update = managed_knowledge_item_crud.update_if_version
    reached_update = 0
    reached_lock = asyncio.Lock()
    release_updates = asyncio.Event()

    async def _synchronized_update(*args, **kwargs):
        nonlocal reached_update
        async with reached_lock:
            reached_update += 1
            if reached_update == 2:
                release_updates.set()
        await asyncio.wait_for(release_updates.wait(), timeout=5)
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(managed_knowledge_item_crud, "update_if_version", _synchronized_update)

    async def _update(content: str) -> str:
        async with session_factory() as session:
            try:
                result = await managed_knowledge_service.update(
                    session,
                    uid="user-1",
                    knowledge_base_id=knowledge_base_id,
                    knowledge_id=knowledge_id,
                    expected_version=1,
                    knowledge_key="race",
                    content=content,
                    source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                    actor=ManagedKnowledgeActorType.LLM,
                )
                assert result.status == ManagedKnowledgeMutationStatus.UPDATED
                return "updated"
            except ManagedKnowledgeConflictError:
                return "conflict"

    try:
        outcomes = await asyncio.gather(_update("worker a"), _update("worker b"))
        async with session_factory() as verify_session:
            current = await verify_session.get(ManagedKnowledgeItem, knowledge_id)
            revisions = list((await verify_session.execute(select(ManagedKnowledgeRevision).where(ManagedKnowledgeRevision.knowledge_id == knowledge_id).order_by(ManagedKnowledgeRevision.version.asc()))).scalars().all())
    finally:
        await engine.dispose()

    assert sorted(outcomes) == ["conflict", "updated"]
    assert current.version == 2
    assert current.content in {"worker a", "worker b"}
    assert [revision.version for revision in revisions] == [1, 2]


@pytest.mark.asyncio
async def test_history_remains_queryable_after_managed_item_row_is_removed(db_session: AsyncSession):
    knowledge_base = await _create_container(db_session, name="history-retention")
    knowledge_base_id = knowledge_base.id
    created = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        knowledge_key="history-retention",
        content="History must outlive the mutable item row.",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )
    knowledge_id = created.item.id

    item = await db_session.get(ManagedKnowledgeItem, knowledge_id)
    await db_session.delete(item)
    await db_session.commit()

    history = await managed_knowledge_service.list_history(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        knowledge_id=knowledge_id,
    )

    assert [revision.version for revision in history] == [1]
    assert history[0].after_snapshot["content"] == "History must outlive the mutable item row."


@pytest.mark.asyncio
async def test_commit_false_validation_failure_does_not_rollback_caller_transaction(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    knowledge_base = await _create_container(db_session, name="transaction-boundary")
    knowledge_base_id = knowledge_base.id
    marker = PromptLibrary(name="caller-transaction-marker", uid="user-1", content="pending")
    db_session.add(marker)
    await db_session.flush()
    monkeypatch.setattr(
        "app.core.knowledge.managed.estimate_tokens",
        lambda _content: MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS + 1,
    )

    with pytest.raises(ManagedKnowledgeContentTooLongError):
        await managed_knowledge_service.create(
            db_session,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
            knowledge_key="too-long-in-outer-tx",
            content="oversized",
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
            commit=False,
        )

    assert db_session.in_transaction()
    await db_session.commit()
    persisted = (await db_session.execute(select(PromptLibrary).where(PromptLibrary.name == "caller-transaction-marker"))).scalars().one()
    assert persisted.content == "pending"


@pytest.mark.asyncio
async def test_concurrent_duplicate_key_returns_existing_state_instead_of_integrity_error(tmp_path):
    database_path = tmp_path / "managed-knowledge-duplicate-race.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=_TABLES))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as setup_session:
        knowledge_base = await _create_container(setup_session, name="duplicate-race")
        knowledge_base_id = knowledge_base.id

    async def _create(content: str) -> ManagedKnowledgeMutationStatus:
        async with session_factory() as session:
            result = await managed_knowledge_service.create(
                session,
                uid="user-1",
                knowledge_base_id=knowledge_base_id,
                knowledge_key="same-key",
                content=content,
                source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                actor=ManagedKnowledgeActorType.LLM,
            )
            return result.status

    try:
        statuses = await asyncio.gather(_create("first concurrent content"), _create("second concurrent content"))
    finally:
        await engine.dispose()

    assert sorted(status.value for status in statuses) == sorted([ManagedKnowledgeMutationStatus.CREATED.value, ManagedKnowledgeMutationStatus.EXISTING_KEY.value])


@pytest.mark.asyncio
async def test_managed_knowledge_id_is_not_reused_after_physical_row_cleanup(db_session: AsyncSession):
    knowledge_base = await _create_container(db_session, name="id-no-reuse")
    knowledge_base_id = knowledge_base.id
    first = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        knowledge_key="first-id",
        content="first content",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )
    first_id = first.item.id

    first_item = await db_session.get(ManagedKnowledgeItem, first_id)
    await db_session.delete(first_item)
    await db_session.commit()

    second = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        knowledge_key="second-id",
        content="second content",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )

    assert second.item.id > first_id


@pytest.mark.asyncio
async def test_duplicate_conflict_current_read_compiles_to_for_update_for_mysql():
    db = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    db.execute.return_value = result

    await managed_knowledge_item_crud.get_by_key(
        db,
        uid="user-1",
        knowledge_base_id=1,
        knowledge_key="same-key",
        current_read=True,
    )

    statement = db.execute.await_args.args[0]
    mysql_compiled = str(statement.compile(dialect=mysql.dialect())).upper()
    sqlite_compiled = str(statement.compile(dialect=sqlite.dialect())).upper()
    assert "FOR UPDATE" in mysql_compiled
    assert "FOR UPDATE" not in sqlite_compiled


@pytest.mark.asyncio
async def test_missing_managed_knowledge_base_uses_dedicated_not_found_error(db_session: AsyncSession):
    with pytest.raises(ManagedKnowledgeNotFoundError) as exc_info:
        await managed_knowledge_service.create(
            db_session,
            uid="user-1",
            knowledge_base_id=999999,
            knowledge_key="missing-container",
            content="content",
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            actor=ManagedKnowledgeActorType.LLM,
        )

    assert exc_info.value.message == "ERR_MANAGED_KNOWLEDGE_BASE_NOT_FOUND"


@pytest.mark.asyncio
async def test_integrity_conflict_recovery_uses_current_read(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    knowledge_base = await _create_container(db_session, name="current-read-recovery")
    existing = ManagedKnowledgeItem(
        id=777,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_key="same-key",
        content="winner",
        content_token_count=1,
        content_hash="0" * 64,
    )
    key_read_modes: list[bool] = []

    async def _get_by_key(
        _db,
        *,
        uid,
        knowledge_base_id,
        knowledge_key,
        current_read=False,
    ):
        key_read_modes.append(current_read)
        return existing if current_read else None

    async def _get_by_content_hash(
        _db,
        *,
        uid,
        knowledge_base_id,
        content_hash,
        current_read=False,
    ):
        return None

    async def _raise_integrity_error(*args, **kwargs):
        raise IntegrityError("INSERT managed_knowledge_item", {}, Exception("duplicate"))

    monkeypatch.setattr(managed_knowledge_item_crud, "get_by_key", _get_by_key)
    monkeypatch.setattr(managed_knowledge_item_crud, "get_by_content_hash", _get_by_content_hash)
    monkeypatch.setattr(managed_knowledge_item_crud, "create", _raise_integrity_error)

    result = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_key="same-key",
        content="loser",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )

    assert result.status == ManagedKnowledgeMutationStatus.EXISTING_KEY
    assert result.item is existing
    assert key_read_modes == [False, True]


@pytest.mark.asyncio
async def test_concurrent_duplicate_content_returns_existing_state_instead_of_integrity_error(tmp_path):
    database_path = tmp_path / "managed-knowledge-duplicate-content-race.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=_TABLES))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as setup_session:
        knowledge_base = await _create_container(setup_session, name="duplicate-content-race")
        knowledge_base_id = knowledge_base.id

    async def _create(knowledge_key: str) -> ManagedKnowledgeMutationStatus:
        async with session_factory() as session:
            result = await managed_knowledge_service.create(
                session,
                uid="user-1",
                knowledge_base_id=knowledge_base_id,
                knowledge_key=knowledge_key,
                content="same concurrent content",
                source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                actor=ManagedKnowledgeActorType.LLM,
            )
            return result.status

    try:
        statuses = await asyncio.gather(_create("first-key"), _create("second-key"))
    finally:
        await engine.dispose()

    assert sorted(status.value for status in statuses) == sorted(
        [
            ManagedKnowledgeMutationStatus.CREATED.value,
            ManagedKnowledgeMutationStatus.EXISTING_CONTENT.value,
        ]
    )


@pytest.mark.asyncio
async def test_user_can_explicitly_reenable_llm_maintenance(db_session: AsyncSession):
    knowledge_base = await _create_container(db_session, name="reenable-maintenance")
    created = await managed_knowledge_service.create(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_key="user-managed",
        content="User maintained content",
        source_type=ManagedKnowledgeSourceType.USER_API,
        actor=ManagedKnowledgeActorType.USER,
    )
    assert created.item.llm_maintainable is False

    unlocked = await managed_knowledge_service.update(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_id=created.item.id,
        expected_version=1,
        knowledge_key="user-managed",
        content="User maintained content",
        source_type=ManagedKnowledgeSourceType.USER_API,
        actor=ManagedKnowledgeActorType.USER,
        llm_maintainable=True,
    )
    assert unlocked.item.version == 2
    assert unlocked.item.llm_maintainable is True
    assert unlocked.item.last_modified_by == ManagedKnowledgeActorType.USER

    llm_updated = await managed_knowledge_service.update(
        db_session,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        knowledge_id=created.item.id,
        expected_version=2,
        knowledge_key="user-managed",
        content="LLM maintained content after explicit user unlock",
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        actor=ManagedKnowledgeActorType.LLM,
    )
    assert llm_updated.status == ManagedKnowledgeMutationStatus.UPDATED
    assert llm_updated.item.version == 3
    assert llm_updated.item.last_modified_by == ManagedKnowledgeActorType.LLM
