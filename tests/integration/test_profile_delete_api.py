from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.models  # noqa: F401
from app.api.v1.profile import router
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.session import session_crud
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseProfileBinding,
    KnowledgeBaseType,
)
from app.models.message import Message, MessageRole
from app.models.message_platform import MessagePlatform, MessagePlatformType
from app.models.profile import Profile
from app.models.scheduled_task import ScheduledTask
from app.models.session import ChatSession
from app.providers.database import get_db


@pytest_asyncio.fixture
async def profile_delete_api(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[
        FastAPI,
        async_sessionmaker[AsyncSession],
        SimpleNamespace,
    ]
]:
    database_path = tmp_path / "profile-delete-api.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")
    current_user = SimpleNamespace(uid="profile-delete-user", is_superuser=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    def override_get_current_user():
        return current_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        yield app, session_factory, current_user
    finally:
        await engine.dispose()


async def _seed_profile_delete_case(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int, int]:
    async with session_factory() as db:
        default_profile = Profile(
            uid="profile-delete-user",
            name="Default Profile",
            configs={},
            is_default=True,
        )
        target_profile = Profile(
            uid="profile-delete-user",
            name="Disposable Profile",
            configs={},
            is_default=False,
        )
        channel = ModelChannel(
            name="profile-delete-channel",
            api_key="test-key",
            model_ids=[],
        )
        db.add_all([default_profile, target_profile, channel])
        await db.flush()

        session = ChatSession(
            session_id="profile-delete-session-1",
            uid="profile-delete-user",
            profile_id=target_profile.id,
            title="Delete me",
        )
        db.add(session)
        await db.flush()
        db.add(
            Message(
                session_id=session.session_id,
                uid=session.uid,
                role=MessageRole.USER,
                profile_id=target_profile.id,
                content="profile deletion test message",
            )
        )
        scheduled_task = ScheduledTask(
            name="Delete scheduled task",
            uid=session.uid,
            session_id=session.session_id,
            profile_id=target_profile.id,
            message="scheduled message",
            interval_seconds=60,
        )
        db.add(scheduled_task)
        platform = MessagePlatform(
            name="Keep platform",
            platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
            uid=session.uid,
            profile_id=target_profile.id,
        )
        db.add(platform)

        knowledge_base = KnowledgeBase(
            uid=session.uid,
            name="Keep user knowledge base",
            embedding_channel_id=channel.id,
            embedding_model_id="embedding-model",
            collection_name="profile-delete-user-kb",
            knowledge_base_type=KnowledgeBaseType.USER,
        )
        db.add(knowledge_base)
        await db.flush()
        db.add(
            KnowledgeBaseProfileBinding(
                knowledge_base_id=knowledge_base.id,
                profile_id=target_profile.id,
                uid=session.uid,
            )
        )
        await db.commit()

        assert target_profile.id is not None
        assert default_profile.id is not None
        assert platform.id is not None
        assert knowledge_base.id is not None
        return (
            target_profile.id,
            default_profile.id,
            platform.id,
            knowledge_base.id,
        )


@pytest.mark.asyncio
async def test_profile_delete_requires_fresh_impact_confirmation_and_cleans_bound_sessions(
    profile_delete_api,
) -> None:
    app, session_factory, _current_user = profile_delete_api
    target_profile_id, default_profile_id, platform_id, knowledge_base_id = await _seed_profile_delete_case(session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        preview_response = await client.post(
            "/api/v1/profiles/delete",
            params={"profile_id": target_profile_id},
        )
        preview_payload = preview_response.json()
        assert preview_response.status_code == 200
        assert preview_payload["code"] == 200
        preview = preview_payload["data"]
        assert preview["requires_confirmation"] is True
        assert preview["sessions"]["count"] == 1
        assert preview["sessions"]["message_count"] == 1
        assert preview["scheduled_tasks"]["count"] == 1
        assert preview["message_platforms"]["count"] == 1
        assert preview["user_knowledge_base_bindings"]["count"] == 1
        first_token = preview["impact_token"]

        async with session_factory() as db:
            db.add(
                ChatSession(
                    session_id="profile-delete-session-2",
                    uid="profile-delete-user",
                    profile_id=default_profile_id,
                    profile_override_id=target_profile_id,
                    title="Override also deleted",
                )
            )
            await db.commit()

        stale_confirmation = await client.post(
            "/api/v1/profiles/delete",
            params={
                "profile_id": target_profile_id,
                "confirm_impact": "true",
                "impact_token": first_token,
            },
        )
        stale_payload = stale_confirmation.json()
        assert stale_confirmation.status_code == 200
        refreshed = stale_payload["data"]
        assert refreshed["requires_confirmation"] is True
        assert refreshed["sessions"]["count"] == 2
        assert refreshed["impact_token"] != first_token

        final_response = await client.post(
            "/api/v1/profiles/delete",
            params={
                "profile_id": target_profile_id,
                "confirm_impact": "true",
                "impact_token": refreshed["impact_token"],
            },
        )
        final_payload = final_response.json()
        assert final_response.status_code == 200
        assert final_payload["code"] == 200
        assert final_payload["data"] == {"requires_confirmation": False}

    async with session_factory() as db:
        assert await db.get(Profile, target_profile_id) is None
        assert await db.get(Profile, default_profile_id) is not None
        assert await db.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.session_id.in_(("profile-delete-session-1", "profile-delete-session-2")))) == 0
        assert await db.scalar(select(func.count()).select_from(Message).where(Message.session_id == "profile-delete-session-1")) == 0
        assert await db.scalar(select(func.count()).select_from(ScheduledTask).where(ScheduledTask.profile_id == target_profile_id)) == 0

        platform = await db.get(MessagePlatform, platform_id)
        assert platform is not None
        assert platform.profile_id is None

        knowledge_base = await db.get(KnowledgeBase, knowledge_base_id)
        assert knowledge_base is not None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(KnowledgeBaseProfileBinding)
                .where(
                    KnowledgeBaseProfileBinding.knowledge_base_id == knowledge_base_id,
                    KnowledgeBaseProfileBinding.profile_id == target_profile_id,
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_profile_delete_lock_prevents_new_session_binding(
    profile_delete_api,
) -> None:
    _app, session_factory, _current_user = profile_delete_api
    async with session_factory() as setup_db:
        profile = Profile(
            uid="profile-delete-user",
            name="Delete while binding",
            configs={},
        )
        setup_db.add(profile)
        await setup_db.commit()
        await setup_db.refresh(profile)
        profile_id = profile.id
    assert profile_id is not None

    async with session_factory() as delete_db:
        locked_profile = await profile_crud.lock_for_runtime_use(
            delete_db,
            profile_id=profile_id,
            uid="profile-delete-user",
        )
        assert locked_profile is not None

        async def bind_session_after_delete():
            async with session_factory() as bind_db:
                result = await session_crud.upsert_profile(
                    bind_db,
                    session_id="must-not-be-created",
                    uid="profile-delete-user",
                    profile_id=profile_id,
                )
                await bind_db.commit()
                return result

        bind_task = asyncio.create_task(bind_session_after_delete())
        await asyncio.sleep(0.05)
        assert bind_task.done() is False

        await profile_crud.delete_locked(
            delete_db,
            profile=locked_profile,
            commit=False,
        )
        await delete_db.commit()

        bind_result = await asyncio.wait_for(bind_task, timeout=5)

    assert bind_result is None
    async with session_factory() as verify_db:
        assert await verify_db.get(Profile, profile_id) is None
        assert (
            await session_crud.get_by_session_id(
                verify_db,
                "must-not-be-created",
            )
            is None
        )
