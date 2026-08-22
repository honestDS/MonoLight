from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core import paths as app_paths
from app.models import KnowledgeBaseCollectionOwner


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: object) -> None:
        pass


class _FakeAsyncSession:
    def __init__(self, name: str, *, enter_error: bool = False) -> None:
        self.name = name
        self.enter_error = enter_error

    async def __aenter__(self) -> _FakeAsyncSession:
        if self.enter_error:
            raise RuntimeError("temporary database connection failure")
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


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
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

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


@pytest.mark.asyncio
async def test_run_collection_cleanup_loop_stops_during_wait_after_first_round(monkeypatch: pytest.MonkeyPatch) -> None:
    session_creation_names: list[str] = []
    process_calls: list[tuple[str, int]] = []
    first_round_completed = asyncio.Event()

    def session_factory() -> _FakeAsyncSession:
        name = f"session-{len(session_creation_names) + 1}"
        session_creation_names.append(name)
        return _FakeAsyncSession(name)

    async def process_pending(db: _FakeAsyncSession, *, limit: int) -> cleanup_service.CollectionCleanupBatchResult:
        process_calls.append((db.name, limit))
        first_round_completed.set()
        return cleanup_service.CollectionCleanupBatchResult(0, 0, 0)

    monkeypatch.setattr(cleanup_service, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(cleanup_service, "process_pending_collection_cleanups", process_pending)

    stop_event = asyncio.Event()
    loop_task = asyncio.create_task(
        cleanup_service.run_knowledge_base_collection_cleanup_loop(
            stop_event,
            interval_seconds=3600,
            batch_limit=7,
        )
    )
    try:
        await asyncio.wait_for(first_round_completed.wait(), timeout=1)
        stop_event.set()
        await asyncio.wait_for(loop_task, timeout=1)
    finally:
        stop_event.set()
        if not loop_task.done():
            loop_task.cancel()
        with suppress(asyncio.CancelledError):
            await loop_task

    assert session_creation_names == ["session-1"]
    assert process_calls == [("session-1", 7)]


@pytest.mark.asyncio
async def test_run_collection_cleanup_loop_recovers_from_session_and_processing_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_creation_names: list[str] = []
    process_calls: list[tuple[str, int]] = []
    successful_round_completed = asyncio.Event()

    def session_factory() -> _FakeAsyncSession:
        name = f"session-{len(session_creation_names) + 1}"
        session_creation_names.append(name)
        return _FakeAsyncSession(name, enter_error=name == "session-1")

    async def process_pending(db: _FakeAsyncSession, *, limit: int) -> cleanup_service.CollectionCleanupBatchResult:
        process_calls.append((db.name, limit))
        if len(process_calls) == 1:
            raise RuntimeError("temporary processing failure")
        successful_round_completed.set()
        return cleanup_service.CollectionCleanupBatchResult(0, 0, 0)

    monkeypatch.setattr(cleanup_service, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(cleanup_service, "process_pending_collection_cleanups", process_pending)

    stop_event = asyncio.Event()
    loop_task = asyncio.create_task(
        cleanup_service.run_knowledge_base_collection_cleanup_loop(
            stop_event,
            interval_seconds=0.001,
            batch_limit=11,
        )
    )
    try:
        await asyncio.wait_for(successful_round_completed.wait(), timeout=1)
        stop_event.set()
        await asyncio.wait_for(loop_task, timeout=1)
    finally:
        stop_event.set()
        if not loop_task.done():
            loop_task.cancel()
        with suppress(asyncio.CancelledError):
            await loop_task

    assert session_creation_names == ["session-1", "session-2", "session-3"]
    assert process_calls == [("session-2", 11), ("session-3", 11)]
