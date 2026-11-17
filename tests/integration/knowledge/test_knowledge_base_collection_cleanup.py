from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from datetime import datetime
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.api.v1.users import router as users_router
from app.core import paths as app_paths
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models import KnowledgeBase, KnowledgeBaseCollectionOwner, LongTermMemoryStore, ModelChannel, User
from app.providers.database import get_db


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: object) -> None:
        pass


with (
    patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient),
    patch.object(app_paths, "ensure_data_dirs"),
):
    cleanup_service = importlib.import_module("app.core.knowledge_jobs.collection_cleanup")
    chroma_module = importlib.import_module("app.providers.vector.chroma")


@pytest_asyncio.fixture()
async def sqlite_session_factory(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory() as factory:
        yield factory


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
async def test_user_delete_registers_and_retries_all_knowledge_collection_cleanups(database_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    normal_uid = "normal-user-uid"
    peer_uid = "peer-user-uid"
    admin_uid = "admin-user-uid"
    normal_kb_collections = ("normal-kb-base", "normal-kb-active", "normal-kb-target", "normal-kb-old")
    normal_memory_collections = ("normal-memory-active", "normal-memory-target", "normal-memory-old")
    peer_kb_collections = ("peer-kb-base", "peer-kb-active", "peer-kb-target", "peer-kb-old")
    peer_memory_collections = ("peer-memory-active", "peer-memory-target", "peer-memory-old")
    normal_collection_names = set(normal_kb_collections + normal_memory_collections)
    peer_collection_names = set(peer_kb_collections + peer_memory_collections)
    failing_collection = normal_memory_collections[-1]

    async with database_factory(foreign_keys=True) as sqlite_session_factory:
        admin_user = User(uid=admin_uid, username="admin-user", is_superuser=True)
        normal_user = User(uid=normal_uid, username="normal-user")
        peer_user = User(uid=peer_uid, username="peer-user")
        channel = ModelChannel(name="shared-channel", api_key="enc:v1:test-api-key")

        async with sqlite_session_factory() as session:
            session.add_all([admin_user, normal_user, peer_user, channel])
            await session.flush()
            channel_id = channel.id
            assert channel_id is not None

            def make_knowledge_base(uid: str, prefix: str, collections: tuple[str, str, str, str]) -> KnowledgeBase:
                return KnowledgeBase(
                    uid=uid,
                    name=f"{prefix}-knowledge-base",
                    embedding_channel_id=channel_id,
                    embedding_model_id="shared-embedding-model",
                    embedding_dimensions=3,
                    collection_name=collections[0],
                    active_embedding_channel_id=channel_id,
                    active_embedding_model_id="shared-embedding-model",
                    active_embedding_dimensions=3,
                    active_embedding_signature=f"{prefix}-active-signature",
                    active_embedding_revision=1,
                    active_collection_name=collections[1],
                    target_embedding_channel_id=channel_id,
                    target_embedding_model_id="shared-embedding-model",
                    target_embedding_dimensions=3,
                    target_embedding_signature=f"{prefix}-target-signature",
                    target_embedding_revision=2,
                    target_collection_name=collections[2],
                    old_collection_name=collections[3],
                )

            normal_knowledge_base = make_knowledge_base(normal_uid, "normal", normal_kb_collections)
            peer_knowledge_base = make_knowledge_base(peer_uid, "peer", peer_kb_collections)
            normal_memory_store = LongTermMemoryStore(
                uid=normal_uid,
                active_collection_name=normal_memory_collections[0],
                target_collection_name=normal_memory_collections[1],
                old_collection_name=normal_memory_collections[2],
            )
            peer_memory_store = LongTermMemoryStore(
                uid=peer_uid,
                active_collection_name=peer_memory_collections[0],
                target_collection_name=peer_memory_collections[1],
                old_collection_name=peer_memory_collections[2],
            )
            session.add_all([normal_knowledge_base, peer_knowledge_base, normal_memory_store, peer_memory_store])
            await session.commit()
            normal_knowledge_base_id = normal_knowledge_base.id
            peer_knowledge_base_id = peer_knowledge_base.id
            assert normal_knowledge_base_id is not None
            assert peer_knowledge_base_id is not None

        assert normal_collection_names.isdisjoint(peer_collection_names)

        app = FastAPI()
        register_handlers(app)
        app.include_router(users_router, prefix="/api/v1/admin")

        async def override_get_db() -> AsyncIterator[AsyncSession]:
            async with sqlite_session_factory() as session:
                yield session

        async def override_get_current_user() -> User:
            return admin_user

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = override_get_current_user

        called_names: list[str] = []
        failed_once = False

        async def delete_collection_if_exists(collection_name: str) -> bool:
            nonlocal failed_once
            called_names.append(collection_name)
            if collection_name == failing_collection and not failed_once:
                failed_once = True
                raise RuntimeError("temporary delete failure")
            return True

        monkeypatch.setattr(cleanup_service, "async_delete_collection_if_exists", delete_collection_if_exists)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/admin/user/delete", params={"uid": normal_uid})

        app.dependency_overrides.clear()

        assert response.status_code == 200
        assert called_names == []

        owners_after_delete = await _get_owners(sqlite_session_factory)
        owners_by_name = {owner.collection_name: owner for owner in owners_after_delete}
        assert set(owners_by_name) == normal_collection_names | set(peer_kb_collections)
        assert normal_collection_names.issubset(owners_by_name)
        assert set(peer_kb_collections).issubset(owners_by_name)
        assert all(owners_by_name[name].knowledge_base_id is None for name in normal_collection_names)
        assert all(owners_by_name[name].knowledge_base_id == peer_knowledge_base_id for name in peer_kb_collections)
        peer_owner_state = {
            name: (
                owners_by_name[name].knowledge_base_id,
                owners_by_name[name].cleanup_revision,
                owners_by_name[name].cleanup_attempt_count,
                owners_by_name[name].cleanup_error,
            )
            for name in peer_kb_collections
        }

        async with sqlite_session_factory() as session:
            assert (await session.execute(select(User).where(User.uid == normal_uid))).scalar_one_or_none() is None
            assert (await session.execute(select(KnowledgeBase).where(KnowledgeBase.uid == normal_uid))).scalar_one_or_none() is None
            assert (await session.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == normal_uid))).scalar_one_or_none() is None
            assert (await session.execute(select(User).where(User.uid == peer_uid))).scalar_one_or_none() is not None
            assert (await session.execute(select(KnowledgeBase).where(KnowledgeBase.uid == peer_uid))).scalar_one_or_none() is not None
            assert (await session.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == peer_uid))).scalar_one_or_none() is not None

        async with sqlite_session_factory() as session:
            first_result = await cleanup_service.process_pending_collection_cleanups(session)

        assert first_result.pending_count == 7
        assert first_result.succeeded_count == 6
        assert first_result.failed_count == 1
        assert len(called_names) == 7
        assert set(called_names) == normal_collection_names
        assert called_names.count(failing_collection) == 1

        owners_after_first_cleanup = await _get_owners(sqlite_session_factory)
        remaining_normal_owners = [owner for owner in owners_after_first_cleanup if owner.collection_name in normal_collection_names]
        assert {owner.collection_name for owner in remaining_normal_owners} == {failing_collection}
        assert remaining_normal_owners[0].knowledge_base_id is None
        assert remaining_normal_owners[0].cleanup_attempt_count == 1
        assert remaining_normal_owners[0].cleanup_error == "RuntimeError: temporary delete failure"
        assert {
            owner.collection_name: (
                owner.knowledge_base_id,
                owner.cleanup_revision,
                owner.cleanup_attempt_count,
                owner.cleanup_error,
            )
            for owner in owners_after_first_cleanup
            if owner.collection_name in peer_kb_collections
        } == peer_owner_state

        async with sqlite_session_factory() as session:
            second_result = await cleanup_service.process_pending_collection_cleanups(session)

        assert second_result.pending_count == 1
        assert second_result.succeeded_count == 1
        assert second_result.failed_count == 0
        assert len(called_names) == 8
        assert called_names[-1] == failing_collection

        async with sqlite_session_factory() as session:
            third_result = await cleanup_service.process_pending_collection_cleanups(session)

        assert third_result.pending_count == 0
        assert third_result.succeeded_count == 0
        assert third_result.failed_count == 0
        assert len(called_names) == 8

        final_owners = await _get_owners(sqlite_session_factory)
        assert {owner.collection_name for owner in final_owners} == set(peer_kb_collections)
        assert not any(owner.collection_name in normal_collection_names for owner in final_owners)
        assert {
            owner.collection_name: (
                owner.knowledge_base_id,
                owner.cleanup_revision,
                owner.cleanup_attempt_count,
                owner.cleanup_error,
            )
            for owner in final_owners
        } == peer_owner_state

        async with sqlite_session_factory() as session:
            assert (await session.execute(select(User).where(User.uid == peer_uid))).scalar_one_or_none() is not None
            assert (await session.execute(select(KnowledgeBase).where(KnowledgeBase.uid == peer_uid))).scalar_one_or_none() is not None
            assert (await session.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == peer_uid))).scalar_one_or_none() is not None
