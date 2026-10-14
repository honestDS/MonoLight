import asyncio
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from sqlmodel import select

from app.api.v1 import knowledge_base, memories, message_platforms, scheduled_tasks, users
from app.core import security, user_file_cleanup
from app.core.constants import ERR_USER_NOT_FOUND
from app.core.i18n import t
from app.core.memory import memory_service, service_create
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseCollectionOwner
from app.models.memory import LongTermMemoryMutationJob, LongTermMemoryRecord, LongTermMemoryStore
from app.models.message_platform import MessagePlatform, MessagePlatformType
from app.models.profile import Profile
from app.models.scheduled_task import ScheduledTask
from app.models.session import ChatSession
from app.models.user import User
from app.providers.database import get_db


@pytest_asyncio.fixture
async def resource_app(database_factory, monkeypatch, tmp_path: Path):
    async with database_factory(foreign_keys=True, wal=True) as session_factory:
        embedding_channel = ModelChannel(
            name="resource-embedding",
            api_key="enc:v1:test-key",
            base_url="https://embedding.test",
            model_ids=[
                {
                    "model_id": "resource-embedding-model",
                    "usage": "EMBEDDING",
                    "protocol": "OPENAI_EMBEDDING",
                    "embedding_dimensions": 3,
                }
            ],
        )
        async with session_factory() as session:
            session.add_all(
                [
                    User(uid="resource-owner", username="resource-owner"),
                    User(uid="resource-admin", username="resource-admin", is_superuser=True),
                    embedding_channel,
                ]
            )
            await session.commit()
            await session.refresh(embedding_channel)

        monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-key")
        monkeypatch.setattr(security, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(security, "get_jwt_secret_key", lambda: "test-jwt-secret")
        monkeypatch.setattr(user_file_cleanup, "TEMP_DIR", tmp_path / "temp")
        monkeypatch.setattr(user_file_cleanup, "AUDIT_DIR", tmp_path / "audit")

        async def get_test_db():
            async with session_factory() as session:
                yield session

        app = FastAPI()
        register_handlers(app)
        app.include_router(users.router, prefix="/api/v1/admin")
        app.include_router(knowledge_base.router, prefix="/api/v1")
        app.dependency_overrides[get_db] = get_test_db

        yield app, session_factory, embedding_channel.id


@pytest.mark.parametrize("role", ["owner", "admin-target"])
@pytest.mark.asyncio
async def test_knowledge_base_creation_concurrent_with_user_deletion(resource_app, monkeypatch, role):
    app, session_factory, embedding_channel_id = resource_app
    collections: set[str] = set()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def create_collection(collection_name: str):
        collections.add(collection_name)
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10)

    async def delete_collection(collection_name: str):
        collections.discard(collection_name)

    monkeypatch.setattr(knowledge_base, "async_create_collection", create_collection)
    monkeypatch.setattr(knowledge_base, "async_delete_collection", delete_collection)

    owner_token = security.create_access_token({"sub": "resource-owner"})
    admin_token = security.create_access_token({"sub": "resource-admin"})
    request_payload = {
        "name": "concurrent-resource",
        "embedding_channel_id": embedding_channel_id,
        "embedding_model_id": "resource-embedding-model",
    }
    if role == "admin-target":
        request_payload["uid"] = "resource-owner"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        create_task = asyncio.create_task(
            client.post(
                "/api/v1/knowledge-base/create",
                json=request_payload,
                headers={"Authorization": f"Bearer {owner_token if role == 'owner' else admin_token}"},
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)

            delete_response = await asyncio.wait_for(
                client.post(
                    "/api/v1/admin/user/delete",
                    params={"uid": "resource-owner"},
                    headers={"Authorization": f"Bearer {admin_token}"},
                ),
                timeout=10,
            )
            assert delete_response.status_code == 200
            assert delete_response.json()["code"] == 200

            release.set()
            create_response = await asyncio.wait_for(create_task, timeout=10)
            assert create_response.status_code == 404
            create_body = create_response.json()
            assert create_body["code"] == 404
            assert create_body["message"] == t(ERR_USER_NOT_FOUND)
            assert collections == set()

            async with session_factory() as verification_session:
                owner_result = await verification_session.execute(select(User).where(User.uid == "resource-owner"))
                knowledge_base_result = await verification_session.execute(select(KnowledgeBase).where(KnowledgeBase.uid == "resource-owner"))
                assert owner_result.scalar_one_or_none() is None
                assert knowledge_base_result.scalar_one_or_none() is None
        finally:
            release.set()
            if not create_task.done():
                create_task.cancel()
            try:
                await asyncio.wait_for(create_task, timeout=5)
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_scheduled_task_creation_concurrent_with_user_deletion(resource_app, monkeypatch):
    app, session_factory, _ = resource_app
    app.include_router(scheduled_tasks.router, prefix="/api/v1")

    async with session_factory() as seed_session:
        profile = Profile(uid="resource-owner", name="resource-profile", configs={})
        seed_session.add(profile)
        await seed_session.flush()
        seed_session.add(
            ChatSession(
                session_id="resource-session",
                uid="resource-owner",
                profile_id=profile.id,
            )
        )
        await seed_session.commit()

    entered = asyncio.Event()
    release = asyncio.Event()
    original_get_session_profile_id = scheduled_tasks._get_session_profile_id

    async def wait_for_session_profile_id(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        return await original_get_session_profile_id(*args, **kwargs)

    monkeypatch.setattr(scheduled_tasks, "_get_session_profile_id", wait_for_session_profile_id)

    owner_token = security.create_access_token({"sub": "resource-owner"})
    admin_token = security.create_access_token({"sub": "resource-admin"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        create_task = asyncio.create_task(
            client.post(
                "/api/v1/scheduled-tasks/create",
                json={
                    "name": "deleted-owner-task",
                    "session_id": "resource-session",
                    "message": "scheduled resource",
                    "interval_seconds": 60,
                },
                headers={"Authorization": f"Bearer {owner_token}"},
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)

            delete_response = await asyncio.wait_for(
                client.post(
                    "/api/v1/admin/user/delete",
                    params={"uid": "resource-owner"},
                    headers={"Authorization": f"Bearer {admin_token}"},
                ),
                timeout=10,
            )
            assert delete_response.status_code == 200
            assert delete_response.json()["code"] == 200

            release.set()
            create_response = await asyncio.wait_for(create_task, timeout=10)
            assert create_response.status_code in {200, 404}
            create_body = create_response.json()
            assert create_body["code"] in {400, 404}
            assert create_body["data"] is None

            async with session_factory() as verification_session:
                owner_result = await verification_session.execute(select(User).where(User.uid == "resource-owner"))
                profile_result = await verification_session.execute(select(Profile).where(Profile.uid == "resource-owner"))
                chat_session_result = await verification_session.execute(select(ChatSession).where(ChatSession.uid == "resource-owner"))
                scheduled_task_result = await verification_session.execute(select(ScheduledTask).where(ScheduledTask.uid == "resource-owner"))
                assert owner_result.scalar_one_or_none() is None
                assert profile_result.scalar_one_or_none() is None
                assert chat_session_result.scalar_one_or_none() is None
                assert scheduled_task_result.scalar_one_or_none() is None
        finally:
            release.set()
            if not create_task.done():
                create_task.cancel()
            try:
                await asyncio.wait_for(create_task, timeout=5)
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_message_platform_creation_concurrent_with_user_deletion(resource_app):
    app, session_factory, _ = resource_app
    app.include_router(message_platforms.router, prefix="/api/v1")

    authenticated = asyncio.Event()
    release = asyncio.Event()

    async def authenticate_and_wait(token: str = Depends(security.oauth2_scheme)):
        user = await security.get_current_user(token)
        if user.uid == "resource-owner":
            authenticated.set()
            await asyncio.wait_for(release.wait(), timeout=10)
        return user

    app.dependency_overrides[security.get_current_user] = authenticate_and_wait

    owner_token = security.create_access_token({"sub": "resource-owner"})
    admin_token = security.create_access_token({"sub": "resource-admin"})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        create_task = asyncio.create_task(
            client.post(
                "/api/v1/message-platforms/create",
                json={"name": "deleted-owner-platform", "config": {}},
                headers={"Authorization": f"Bearer {owner_token}"},
            )
        )
        try:
            await asyncio.wait_for(authenticated.wait(), timeout=5)

            delete_response = await asyncio.wait_for(
                client.post(
                    "/api/v1/admin/user/delete",
                    params={"uid": "resource-owner"},
                    headers={"Authorization": f"Bearer {admin_token}"},
                ),
                timeout=10,
            )
            assert delete_response.status_code == 200
            assert delete_response.json()["code"] == 200

            release.set()
            create_response = await asyncio.wait_for(create_task, timeout=10)
            assert create_response.status_code == 404
            create_body = create_response.json()
            assert create_body["code"] == 404
            assert create_body["message"] == t(ERR_USER_NOT_FOUND)

            async with session_factory() as verification_session:
                owner_result = await verification_session.execute(select(User).where(User.uid == "resource-owner"))
                message_platform_result = await verification_session.execute(select(MessagePlatform).where(MessagePlatform.uid == "resource-owner"))
                assert owner_result.scalar_one_or_none() is None
                assert message_platform_result.scalar_one_or_none() is None
        finally:
            release.set()
            if not create_task.done():
                create_task.cancel()
            try:
                await asyncio.wait_for(create_task, timeout=5)
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_memory_creation_replay_concurrent_with_user_deletion(resource_app, monkeypatch):
    app, session_factory, embedding_channel_id = resource_app
    app.include_router(memories.router, prefix="/api/v1")

    async with session_factory() as seed_session:
        seed_session.add(
            LongTermMemoryStore(
                uid="resource-owner",
                active_embedding_channel_id=embedding_channel_id,
                active_embedding_model_id="resource-embedding-model",
                active_embedding_dimensions=3,
                active_embedding_signature="resource-signature",
                active_embedding_revision=1,
                active_collection_name="resource-memory",
                index_status="ready",
            )
        )
        await seed_session.commit()
        await memory_service.create(
            seed_session,
            uid="resource-owner",
            dedupe_key="replayed-create",
            content="remember resource",
            memory_key="resource.key",
            memory_type="fact",
            commit=True,
        )

    entered = asyncio.Event()
    release = asyncio.Event()
    original_accept_existing_job = service_create._accept_existing_job

    async def wait_for_replayed_job(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        return await original_accept_existing_job(*args, **kwargs)

    monkeypatch.setattr(service_create, "_accept_existing_job", wait_for_replayed_job)

    owner_token = security.create_access_token({"sub": "resource-owner"})
    admin_token = security.create_access_token({"sub": "resource-admin"})
    request_payload = {
        "dedupe_key": "replayed-create",
        "content": "remember resource",
        "memory_key": "resource.key",
        "memory_type": "fact",
    }
    tasks: list[asyncio.Task] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        create_task = asyncio.create_task(
            client.post(
                "/api/v1/memories/create",
                json=request_payload,
                headers={"Authorization": f"Bearer {owner_token}"},
            )
        )
        tasks.append(create_task)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)

            delete_task = asyncio.create_task(
                client.post(
                    "/api/v1/admin/user/delete",
                    params={"uid": "resource-owner"},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
            )
            tasks.append(delete_task)
            await asyncio.wait({delete_task}, timeout=1)

            release.set()
            create_response, delete_response = await asyncio.wait_for(asyncio.gather(create_task, delete_task), timeout=10)
            assert create_response.status_code == 200
            assert create_response.json()["code"] == 200
            assert delete_response.status_code == 200
            assert delete_response.json()["code"] == 200

            async with session_factory() as verification_session:
                owner_result = await verification_session.execute(select(User).where(User.uid == "resource-owner"))
                store_result = await verification_session.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == "resource-owner"))
                job_result = await verification_session.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == "resource-owner"))
                record_result = await verification_session.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == "resource-owner"))
                assert owner_result.scalar_one_or_none() is None
                assert store_result.scalar_one_or_none() is None
                assert job_result.scalar_one_or_none() is None
                assert record_result.scalar_one_or_none() is None
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
                except TimeoutError:
                    pass


@pytest.mark.asyncio
async def test_knowledge_base_creation_holds_user_lock_until_commit_concurrent_with_user_deletion(resource_app, monkeypatch):
    app, session_factory, embedding_channel_id = resource_app
    collections: set[str] = set()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def create_collection(collection_name: str):
        collections.add(collection_name)

    async def delete_collection(collection_name: str):
        collections.discard(collection_name)

    monkeypatch.setattr(knowledge_base, "async_create_collection", create_collection)
    monkeypatch.setattr(knowledge_base, "async_delete_collection", delete_collection)

    original_create = knowledge_base.knowledge_base_crud.create

    async def wait_for_create(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(knowledge_base.knowledge_base_crud, "create", wait_for_create)

    owner_token = security.create_access_token({"sub": "resource-owner"})
    admin_token = security.create_access_token({"sub": "resource-admin"})
    tasks: list[asyncio.Task] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        create_task = asyncio.create_task(
            client.post(
                "/api/v1/knowledge-base/create",
                json={
                    "name": "concurrent-resource-locked",
                    "embedding_channel_id": embedding_channel_id,
                    "embedding_model_id": "resource-embedding-model",
                },
                headers={"Authorization": f"Bearer {owner_token}"},
            )
        )
        tasks.append(create_task)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)

            delete_task = asyncio.create_task(
                client.post(
                    "/api/v1/admin/user/delete",
                    params={"uid": "resource-owner"},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
            )
            tasks.append(delete_task)
            await asyncio.wait({delete_task}, timeout=1)

            release.set()
            create_response, delete_response = await asyncio.wait_for(asyncio.gather(create_task, delete_task), timeout=10)
            assert create_response.status_code == 200
            assert create_response.json()["code"] == 200
            assert delete_response.status_code == 200
            assert delete_response.json()["code"] == 200

            collection_name = next(iter(collections))
            async with session_factory() as verification_session:
                owner_result = await verification_session.execute(select(User).where(User.uid == "resource-owner"))
                knowledge_base_result = await verification_session.execute(select(KnowledgeBase).where(KnowledgeBase.uid == "resource-owner"))
                collection_owner_result = await verification_session.execute(select(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.collection_name == collection_name))
                collection_owner = collection_owner_result.scalar_one_or_none()
                assert owner_result.scalar_one_or_none() is None
                assert knowledge_base_result.scalar_one_or_none() is None
                assert collection_owner is not None
                assert collection_owner.knowledge_base_id is None
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
                except TimeoutError:
                    pass


@pytest.mark.asyncio
async def test_message_platform_creation_holds_user_lock_until_commit_concurrent_with_user_deletion(resource_app, monkeypatch):
    app, session_factory, _ = resource_app
    app.include_router(message_platforms.router, prefix="/api/v1")

    entered = asyncio.Event()
    release = asyncio.Event()
    original_create = message_platforms.message_platform_crud.create

    async def wait_for_create(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(message_platforms.message_platform_crud, "create", wait_for_create)

    owner_token = security.create_access_token({"sub": "resource-owner"})
    admin_token = security.create_access_token({"sub": "resource-admin"})
    tasks: list[asyncio.Task] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        create_task = asyncio.create_task(
            client.post(
                "/api/v1/message-platforms/create",
                json={"name": "locked-platform", "config": {}},
                headers={"Authorization": f"Bearer {owner_token}"},
            )
        )
        tasks.append(create_task)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)

            delete_task = asyncio.create_task(
                client.post(
                    "/api/v1/admin/user/delete",
                    params={"uid": "resource-owner"},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
            )
            tasks.append(delete_task)
            await asyncio.wait({delete_task}, timeout=1)

            release.set()
            create_response, delete_response = await asyncio.wait_for(asyncio.gather(create_task, delete_task), timeout=10)
            assert create_response.status_code == 200
            assert create_response.json()["code"] == 200
            assert delete_response.status_code == 200
            assert delete_response.json()["code"] == 200

            async with session_factory() as verification_session:
                owner_result = await verification_session.execute(select(User).where(User.uid == "resource-owner"))
                message_platform_result = await verification_session.execute(select(MessagePlatform).where(MessagePlatform.uid == "resource-owner"))
                assert owner_result.scalar_one_or_none() is None
                assert message_platform_result.scalar_one_or_none() is None
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
                except TimeoutError:
                    pass


@pytest.mark.asyncio
async def test_message_platform_transfer_holds_target_user_lock_until_commit_concurrent_with_user_deletion(resource_app, monkeypatch):
    app, session_factory, _ = resource_app
    app.include_router(message_platforms.router, prefix="/api/v1")

    async with session_factory() as seed_session:
        platform = MessagePlatform(
            name="transfer-target-platform",
            platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
            uid="resource-admin",
            profile_id=None,
            is_enabled=False,
        )
        seed_session.add(platform)
        await seed_session.commit()
        await seed_session.refresh(platform)
        platform_id = platform.id
    assert platform_id is not None

    entered = asyncio.Event()
    release = asyncio.Event()
    target_lock_paused = False
    original_get_by_uid = message_platforms.user_crud.get_by_uid

    async def wait_for_target_user_lock(*args, **kwargs):
        nonlocal target_lock_paused
        user = await original_get_by_uid(*args, **kwargs)
        uid = args[1] if len(args) > 1 else kwargs.get("uid")
        if uid == "resource-owner" and kwargs.get("lock_for_reference_write", False) and not target_lock_paused:
            target_lock_paused = True
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=10)
        return user

    monkeypatch.setattr(message_platforms.user_crud, "get_by_uid", wait_for_target_user_lock)

    admin_token = security.create_access_token({"sub": "resource-admin"})
    tasks: list[asyncio.Task] = []

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        transfer_task = asyncio.create_task(
            client.post(
                "/api/v1/message-platforms/update",
                params={"platform_id": platform_id},
                json={"uid": "resource-owner", "profile_id": None, "is_enabled": False},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        )
        tasks.append(transfer_task)
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)

            delete_task = asyncio.create_task(
                client.post(
                    "/api/v1/admin/user/delete",
                    params={"uid": "resource-owner"},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
            )
            tasks.append(delete_task)
            await asyncio.wait({delete_task}, timeout=1)
            assert not delete_task.done()

            release.set()
            transfer_response, delete_response = await asyncio.wait_for(asyncio.gather(transfer_task, delete_task), timeout=10)
            assert transfer_response.status_code == 200
            assert transfer_response.json()["code"] == 200
            assert delete_response.status_code == 200
            assert delete_response.json()["code"] == 200

            async with session_factory() as verification_session:
                owner_result = await verification_session.execute(select(User).where(User.uid == "resource-owner"))
                platform_result = await verification_session.execute(select(MessagePlatform).where(MessagePlatform.id == platform_id))
                assert owner_result.scalar_one_or_none() is None
                assert platform_result.scalar_one_or_none() is None
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
                except TimeoutError:
                    pass
