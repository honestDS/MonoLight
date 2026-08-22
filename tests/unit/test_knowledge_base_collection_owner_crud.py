"""知识库 collection owner CRUD 和基础设施测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

import app.models as exported_models
from app.core.crud.knowledge_base import knowledge_base_collection_owner_crud
from app.models import KnowledgeBaseCollectionOwner
from app.models.knowledge_base import KnowledgeBase


def _value_for_required_channel_column(column) -> object:
    enum_class = getattr(column.type, "enum_class", None)
    if enum_class is not None:
        return next(iter(enum_class)).value

    try:
        python_type = column.type.python_type
    except (AttributeError, NotImplementedError):
        return "test"

    if python_type is bool:
        return False
    if python_type is int:
        return 1
    if python_type is float:
        return 1.0
    if python_type is datetime:
        return datetime(2026, 8, 23)
    if python_type is bytes:
        return b"test"
    if python_type is dict:
        return {}
    if python_type is list:
        return []
    return f"test-{column.name}"


def _channel_seed_values() -> dict[str, object]:
    channel_table = SQLModel.metadata.tables["channel"]
    values: dict[str, object] = {"id": 1}
    for column in channel_table.columns:
        if column.name == "id" or column.nullable or column.default is not None or column.server_default is not None:
            continue
        values[column.name] = _value_for_required_channel_column(column)
    return values


async def _seed_channel(session_factory: async_sessionmaker[AsyncSession]) -> None:
    channel_table = SQLModel.metadata.tables["channel"]
    async with session_factory() as session:
        await session.execute(insert(channel_table).values(_channel_seed_values()))
        await session.commit()


@pytest_asyncio.fixture()
async def sqlite_database(tmp_path: Path) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    database_path = tmp_path / "knowledge-base-collection-owner-crud.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await _seed_channel(session_factory)
    try:
        yield engine, session_factory
    finally:
        await engine.dispose()


async def _create_knowledge_base(session: AsyncSession, collection_name: str, *, uid: str = "user-a") -> KnowledgeBase:
    knowledge_base = KnowledgeBase(
        uid=uid,
        name=f"Knowledge base {collection_name}",
        embedding_channel_id=1,
        embedding_model_id="test-model",
        collection_name=collection_name,
    )
    session.add(knowledge_base)
    await session.commit()
    await session.refresh(knowledge_base)
    session.add(KnowledgeBaseCollectionOwner(collection_name=collection_name, knowledge_base_id=knowledge_base.id))
    await session.commit()
    return knowledge_base


async def _get_owner(session: AsyncSession, collection_name: str) -> KnowledgeBaseCollectionOwner | None:
    result = await session.execute(select(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.collection_name == collection_name))
    return result.scalar_one_or_none()


def test_collection_owner_is_explicitly_exported_and_registered() -> None:
    assert exported_models.KnowledgeBaseCollectionOwner is KnowledgeBaseCollectionOwner
    assert SQLModel.metadata.tables["knowledge_base_collection_owner"] is KnowledgeBaseCollectionOwner.__table__


@pytest.mark.asyncio
async def test_collection_owner_crud_covers_active_and_pending_lifecycle(sqlite_database) -> None:
    _engine, session_factory = sqlite_database

    async with session_factory() as session:
        knowledge_base_a = await _create_knowledge_base(session, "collection-a")
        knowledge_base_b = await _create_knowledge_base(session, "collection-b", uid="user-b")

        assert await knowledge_base_collection_owner_crud.enqueue(
            session,
            knowledge_base_id=knowledge_base_a.id,
            collection_names=[None, "", "queued-a", "queued-a", "queued-b"],
        ) == ["queued-a", "queued-b"]
        assert await knowledge_base_collection_owner_crud.enqueue(
            session,
            knowledge_base_id=knowledge_base_a.id,
            collection_names=["queued-a", "queued-b", "queued-a"],
        ) == ["queued-a", "queued-b"]
        await session.commit()

        assert (
            await knowledge_base_collection_owner_crud.mark_failed(
                session,
                collection_name="collection-b",
                error="active failure should be ignored",
            )
            is False
        )
        assert await knowledge_base_collection_owner_crud.mark_succeeded(session, collection_name="collection-b") is False
        active_owner = await _get_owner(session, "collection-b")
        assert active_owner is not None
        assert active_owner.knowledge_base_id == knowledge_base_b.id
        assert active_owner.cleanup_attempt_count == 0
        assert active_owner.cleanup_error is None

        await session.delete(knowledge_base_a)
        await session.commit()

        pending = await knowledge_base_collection_owner_crud.list_pending(session, limit=100)
        assert {owner.collection_name for owner in pending} == {"collection-a", "queued-a", "queued-b"}
        assert all(owner.knowledge_base_id is None for owner in pending)
        assert await knowledge_base_collection_owner_crud.list_pending(session, limit=0) == []
        assert await knowledge_base_collection_owner_crud.list_pending(session, limit=-1) == []

        limited_pending = await knowledge_base_collection_owner_crud.list_pending(session, limit=2)
        assert len(limited_pending) == 2
        assert {owner.collection_name for owner in limited_pending} <= {"collection-a", "queued-a", "queued-b"}
        assert all(owner.knowledge_base_id is None for owner in limited_pending)

        assert (
            await knowledge_base_collection_owner_crud.mark_failed(
                session,
                collection_name="queued-a",
                error="first cleanup error",
            )
            is True
        )
        assert (
            await knowledge_base_collection_owner_crud.mark_failed(
                session,
                collection_name="queued-a",
                error="latest cleanup error",
            )
            is True
        )
        failed_owner = await _get_owner(session, "queued-a")
        assert failed_owner is not None
        assert failed_owner.knowledge_base_id is None
        assert failed_owner.cleanup_attempt_count == 2
        assert failed_owner.cleanup_error == "latest cleanup error"

        assert await knowledge_base_collection_owner_crud.mark_succeeded(session, collection_name="queued-a") is True
        assert await knowledge_base_collection_owner_crud.mark_succeeded(session, collection_name="queued-a") is False
        assert await _get_owner(session, "queued-a") is None
        assert await _get_owner(session, "collection-b") is not None


@pytest.mark.asyncio
async def test_pending_collection_name_cannot_be_reassigned_after_failure(sqlite_database) -> None:
    _engine, session_factory = sqlite_database

    async with session_factory() as session:
        knowledge_base_a = await _create_knowledge_base(session, "collection-a")
        assert await knowledge_base_collection_owner_crud.enqueue(
            session,
            knowledge_base_id=knowledge_base_a.id,
            collection_names=["retained-collection"],
        ) == ["retained-collection"]
        await session.commit()

        await session.delete(knowledge_base_a)
        await session.commit()
        assert (
            await knowledge_base_collection_owner_crud.mark_failed(
                session,
                collection_name="retained-collection",
                error="preserve this failure",
            )
            is True
        )

        knowledge_base_b = await _create_knowledge_base(session, "collection-b", uid="user-b")
        with pytest.raises(ValueError):
            await knowledge_base_collection_owner_crud.enqueue(
                session,
                knowledge_base_id=knowledge_base_b.id,
                collection_names=["retained-collection"],
            )
        await session.rollback()

    async with session_factory() as session:
        retained_owner = await _get_owner(session, "retained-collection")
        assert retained_owner is not None
        assert retained_owner.knowledge_base_id is None
        assert retained_owner.cleanup_attempt_count == 1
        assert retained_owner.cleanup_error == "preserve this failure"
