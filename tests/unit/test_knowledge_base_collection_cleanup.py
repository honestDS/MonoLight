from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

from app.core import paths as app_paths
from app.models import KnowledgeBaseCollectionOwner
from tests.database_support import clone_sqlite_schema


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: object) -> None:
        pass


with (
    patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient),
    patch.object(app_paths, "ensure_data_dirs"),
):
    cleanup_service = importlib.import_module("app.core.knowledge_base_collection_cleanup")
    chroma_module = importlib.import_module("app.providers.vector.chroma")


@pytest_asyncio.fixture()
async def sqlite_session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "knowledge-base-collection-cleanup.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    await clone_sqlite_schema(database_path)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _insert_pending_owners(session_factory: async_sessionmaker[AsyncSession], names: list[str]) -> None:
    timestamp = datetime(2026, 1, 1)
    async with session_factory() as session:
        session.add_all(
            [
                KnowledgeBaseCollectionOwner(
                    collection_name=name,
                    knowledge_base_id=None,
                    cleanup_attempt_count=0,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                for name in names
            ]
        )
        await session.commit()


async def _get_owners(session_factory: async_sessionmaker[AsyncSession]) -> list[KnowledgeBaseCollectionOwner]:
    async with session_factory() as session:
        result = await session.execute(select(KnowledgeBaseCollectionOwner).order_by(KnowledgeBaseCollectionOwner.collection_name))
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_async_delete_collection_if_exists_returns_true_and_false_for_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    called_names: list[str] = []

    def delete_collection(collection_name: str) -> None:
        called_names.append(collection_name)

    monkeypatch.setattr(chroma_module, "delete_collection", delete_collection)

    assert await chroma_module.async_delete_collection_if_exists("existing-collection") is True
    assert called_names == ["existing-collection"]

    def delete_missing_collection(collection_name: str) -> None:
        called_names.append(collection_name)
        raise chromadb.errors.NotFoundError("collection does not exist")

    monkeypatch.setattr(chroma_module, "delete_collection", delete_missing_collection)

    assert await chroma_module.async_delete_collection_if_exists("missing-collection") is False
    assert called_names == ["existing-collection", "missing-collection"]


@pytest.mark.asyncio
async def test_process_pending_collection_cleanups_removes_deleted_and_missing_owners(sqlite_session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    await _insert_pending_owners(sqlite_session_factory, ["existing-collection", "missing-collection"])
    called_names: list[str] = []

    async def delete_collection_if_exists(collection_name: str) -> bool:
        called_names.append(collection_name)
        return collection_name == "existing-collection"

    monkeypatch.setattr(cleanup_service, "async_delete_collection_if_exists", delete_collection_if_exists)

    async with sqlite_session_factory() as session:
        result = await cleanup_service.process_pending_collection_cleanups(session)

    assert result.pending_count == 2
    assert result.succeeded_count == 2
    assert result.failed_count == 0
    assert called_names == ["existing-collection", "missing-collection"]
    assert await _get_owners(sqlite_session_factory) == []


@pytest.mark.asyncio
async def test_process_pending_collection_cleanups_deletes_outside_database_transaction(sqlite_session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    await _insert_pending_owners(sqlite_session_factory, ["transaction-free-collection"])
    transaction_states: list[bool] = []

    async with sqlite_session_factory() as session:

        async def delete_collection_if_exists(collection_name: str) -> bool:
            transaction_states.append(session.in_transaction())
            return True

        monkeypatch.setattr(cleanup_service, "async_delete_collection_if_exists", delete_collection_if_exists)

        result = await cleanup_service.process_pending_collection_cleanups(session)

    assert result.pending_count == 1
    assert result.succeeded_count == 1
    assert result.failed_count == 0
    assert transaction_states == [False]
    assert await _get_owners(sqlite_session_factory) == []


@pytest.mark.asyncio
async def test_process_pending_collection_cleanups_continues_after_failure_and_retries(sqlite_session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    names = ["01-failing-collection", "02-missing-collection", "03-existing-collection"]
    await _insert_pending_owners(sqlite_session_factory, names)
    called_names: list[str] = []

    async def delete_collection_if_exists(collection_name: str) -> bool:
        called_names.append(collection_name)
        if collection_name == "01-failing-collection":
            raise RuntimeError("temporary delete failure")
        return collection_name == "03-existing-collection"

    monkeypatch.setattr(cleanup_service, "async_delete_collection_if_exists", delete_collection_if_exists)

    async with sqlite_session_factory() as session:
        first_result = await cleanup_service.process_pending_collection_cleanups(session)

    assert first_result.pending_count == 3
    assert first_result.succeeded_count == 2
    assert first_result.failed_count == 1
    assert called_names == names

    remaining_owners = await _get_owners(sqlite_session_factory)
    assert [owner.collection_name for owner in remaining_owners] == ["01-failing-collection"]
    assert remaining_owners[0].knowledge_base_id is None
    assert remaining_owners[0].cleanup_attempt_count == 1
    assert remaining_owners[0].cleanup_error == "RuntimeError: temporary delete failure"

    async def delete_collection_after_retry(collection_name: str) -> bool:
        called_names.append(collection_name)
        return False

    monkeypatch.setattr(cleanup_service, "async_delete_collection_if_exists", delete_collection_after_retry)

    async with sqlite_session_factory() as session:
        second_result = await cleanup_service.process_pending_collection_cleanups(session)

    assert second_result.pending_count == 1
    assert second_result.succeeded_count == 1
    assert second_result.failed_count == 0
    assert called_names == [*names, "01-failing-collection"]
    assert await _get_owners(sqlite_session_factory) == []

    async with sqlite_session_factory() as session:
        third_result = await cleanup_service.process_pending_collection_cleanups(session)

    assert third_result.pending_count == 0
    assert third_result.succeeded_count == 0
    assert third_result.failed_count == 0
    assert called_names == [*names, "01-failing-collection"]
