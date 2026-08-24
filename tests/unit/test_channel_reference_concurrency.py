import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.api.v1 import channels
from app.api.v1 import profile as profile_api
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseProfileBinding,
)
from app.models.memory import (
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryMutationJob,
    LongTermMemoryStore,
)
from app.models.profile import Profile, ProfileConfig, ProfileCreate
from app.models.prompt import PromptLibrary


@pytest.mark.asyncio
async def test_profile_creation_and_channel_deletion_serialize_on_channel_lock(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'channel-reference-concurrency.sqlite3').as_posix()}",
        connect_args={"timeout": 5},
        pool_size=5,
        max_overflow=0,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    profile_task = delete_task = None
    release = asyncio.Event()

    try:
        tables = [
            ModelChannel.__table__,
            PromptLibrary.__table__,
            Profile.__table__,
            KnowledgeBase.__table__,
            KnowledgeBaseProfileBinding.__table__,
            KnowledgeBaseCollectionOwner.__table__,
            LongTermMemoryStore.__table__,
            LongTermMemoryEmbeddingRevision.__table__,
            LongTermMemoryMutationJob.__table__,
        ]
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=tables))

        model_id = "serializable-chat-model"
        async with session_factory() as setup_db:
            channel = ModelChannel(
                name="serializable-channel",
                api_key="enc:v1:test-key",
                base_url="https://example.invalid/v1",
                model_ids=[
                    {
                        "model_id": model_id,
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "is_enabled": True,
                        "context_window_k": 128,
                        "max_tokens": 4096,
                        "temperature": 0.0,
                        "top_p": 1.0,
                        "advanced_settings": {},
                    }
                ],
            )
            setup_db.add(channel)
            await setup_db.commit()
            channel_id = channel.id

        profile_in = ProfileCreate(
            name="channel-reference-race-profile",
            configs=ProfileConfig.model_validate(
                {
                    "channel": {
                        "chat_channel": {
                            "rules": [
                                {
                                    "channel_id": channel_id,
                                    "model_id": model_id,
                                    "priority": 1,
                                    "weight": 100,
                                }
                            ]
                        }
                    }
                }
            ).model_dump(),
        )
        user = SimpleNamespace(uid="profile-user", is_superuser=False)
        profile_checked = asyncio.Event()
        delete_lock_attempted = asyncio.Event()
        scan_started = asyncio.Event()

        original_validate = profile_api.validate_audit_model_config

        async def _wait_after_validation(*args, **kwargs):
            result = await original_validate(*args, **kwargs)
            profile_checked.set()
            await release.wait()
            return result

        monkeypatch.setattr(
            profile_api,
            "validate_audit_model_config",
            _wait_after_validation,
        )

        async def _create_profile():
            async with session_factory() as db:
                return await profile_api.create_profile(
                    profile_in,
                    db=db,
                    current_user=user,
                )

        profile_task = asyncio.create_task(_create_profile())
        await asyncio.wait_for(profile_checked.wait(), timeout=5)

        original_lock = channels.channel_crud.lock_for_mutation

        async def _record_delete_lock(*args, **kwargs):
            delete_lock_attempted.set()
            return await original_lock(*args, **kwargs)

        monkeypatch.setattr(
            channels.channel_crud,
            "lock_for_mutation",
            _record_delete_lock,
        )
        original_scan = channels.assert_channel_not_referenced

        async def _record_scan(*args, **kwargs):
            scan_started.set()
            return await original_scan(*args, **kwargs)

        monkeypatch.setattr(
            channels,
            "assert_channel_not_referenced",
            _record_scan,
        )

        async def _delete_channel():
            async with session_factory() as db:
                return await channels.delete_channel(
                    channel_id,
                    db=db,
                    admin={},
                )

        delete_task = asyncio.create_task(_delete_channel())
        await asyncio.wait_for(delete_lock_attempted.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not scan_started.is_set()

        release.set()
        profile_response, delete_response = await asyncio.wait_for(
            asyncio.gather(profile_task, delete_task),
            timeout=10,
        )
        assert profile_response.code == 200
        assert delete_response.code == 200

        async with session_factory() as verify_db:
            assert await verify_db.get(ModelChannel, channel_id) is None
            saved_profile = (await verify_db.execute(select(Profile).where(Profile.name == "channel-reference-race-profile"))).scalar_one()
            saved_config = ProfileConfig.model_validate(saved_profile.configs)
            assert saved_config.channel.chat_channel.rules == []
            if hasattr(saved_config.channel, "context_summary_channel"):
                assert saved_config.channel.context_summary_channel.rules == []
    finally:
        release.set()
        tasks = [task for task in (profile_task, delete_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5,
                )
            except TimeoutError:
                pass
        await engine.dispose()

@pytest.mark.asyncio
async def test_profile_update_lock_includes_previous_and_new_channel_references(monkeypatch):
    locked_ids = []

    async def _fake_lock_many_for_mutation(db, *, channel_ids, commit=False):
        locked_ids.extend(channel_ids)
        return {}

    monkeypatch.setattr(
        profile_api.lock_profile_channel_references.__globals__["channel_crud"],
        "lock_many_for_mutation",
        _fake_lock_many_for_mutation,
    )

    old_channel_id = 10
    new_channel_id = 20
    configs = ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "rules": [
                        {
                            "channel_id": new_channel_id,
                            "model_id": "new-model",
                            "priority": 1,
                            "weight": 100,
                        }
                    ]
                }
            }
        }
    ).model_dump()

    await profile_api.lock_profile_channel_references(
        db=None,
        configs=configs,
        memory_organization=None,
        extra_channel_ids=[old_channel_id],
    )

    assert old_channel_id in locked_ids
    assert new_channel_id in locked_ids
