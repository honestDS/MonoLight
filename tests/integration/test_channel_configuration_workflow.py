from collections.abc import AsyncGenerator
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

import app.core.crypto as crypto_module
from app.api.v1.channels import router as channels_router
from app.api.v1.memories import router as memories_router
from app.core.crud.channel.channel import channel_crud
from app.core.crud.memory.store import memory_store_crud
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import KnowledgeBase
from app.models.memory import (
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryMutationJob,
    LongTermMemoryRecord,
    LongTermMemoryStore,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.providers.database import get_db

API_TABLES = [
    ModelChannel.__table__,
    PromptLibrary.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    LongTermMemoryStore.__table__,
    LongTermMemoryRecord.__table__,
    LongTermMemoryEmbeddingRevision.__table__,
    LongTermMemoryMutationJob.__table__,
]


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"\x00" * 32)


@pytest_asyncio.fixture
async def channel_config_db() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=API_TABLES,
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def channel_config_app(channel_config_db: AsyncSession) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(channels_router, prefix="/api/v1")
    app.include_router(memories_router, prefix="/api/v1")
    current_user = SimpleNamespace(uid="channel-workflow-user", is_superuser=True)

    async def override_get_db():
        yield channel_config_db

    def override_get_current_user():
        return current_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return app


def _chat_model(model_id: str, *, description: str = "organization model") -> dict:
    return {
        "model_id": model_id,
        "usage": "CHAT",
        "protocol": "OPENAI",
        "context_window_k": 64,
        "max_tokens": 20_000,
        "temperature": 0.25,
        "top_p": 0.8,
        "is_enabled": True,
        "description": description,
    }


def _assert_standard(response: httpx.Response, code: int) -> dict:
    payload = response.json()
    assert response.status_code == code
    assert payload["code"] == code
    assert set(payload) == {"code", "message", "data"}
    return payload


@pytest.mark.asyncio
async def test_channel_configuration_lifecycle_syncs_referenced_model_and_protects_channel_deletion(
    channel_config_app: FastAPI,
    channel_config_db: AsyncSession,
) -> None:
    channel_config_db.add(LongTermMemoryStore(uid="channel-workflow-user"))
    await channel_config_db.commit()

    old_model = _chat_model("organization-chat")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=channel_config_app),
        base_url="http://test",
    ) as client:
        created = _assert_standard(
            await client.post(
                "/api/v1/channels/create",
                json={
                    "name": "organization-channel",
                    "api_key": "organization-api-key",
                    "base_url": "https://llm.example/v1",
                    "is_active": True,
                    "model_ids": [old_model],
                },
            ),
            200,
        )
        channel_id = created["data"]["id"]
        assert created["data"]["api_key"] == "organization-api-key"

        settings = _assert_standard(
            await client.post(
                "/api/v1/memories/settings",
                json={
                    "auto_organize_enabled": True,
                    "organization_channel_id": channel_id,
                    "organization_model_id": "organization-chat",
                },
            ),
            200,
        )
        assert settings["data"]["organization"]["model"]["channel_id"] == channel_id
        assert settings["data"]["organization"]["model"]["model_id"] == "organization-chat"

        metadata_model = _chat_model(
            "organization-chat",
            description="updated organization description",
        )
        metadata_update = _assert_standard(
            await client.post(
                "/api/v1/channels/update",
                params={"channel_id": channel_id},
                json={"model_ids": [metadata_model]},
            ),
            200,
        )
        assert metadata_update["data"]["requires_confirmation"] is False
        assert metadata_update["data"]["channel"]["model_ids"][0]["description"] == "updated organization description"

        renamed_model = _chat_model(
            "renamed-organization-chat",
            description="updated organization description",
        )
        preview = _assert_standard(
            await client.post(
                "/api/v1/channels/update",
                params={"channel_id": channel_id},
                json={"model_ids": [renamed_model]},
            ),
            200,
        )
        assert preview["data"]["requires_confirmation"] is True
        assert preview["data"]["synced_memory_organization_settings"] == 1
        impact_token = preview["data"]["config_impact_token"]
        assert isinstance(impact_token, str) and len(impact_token) == 64

        before_confirm = _assert_standard(
            await client.get("/api/v1/channels/get", params={"channel_id": channel_id}),
            200,
        )
        assert before_confirm["data"]["model_ids"][0]["model_id"] == "organization-chat"
        store = await memory_store_crud.get_by_uid(channel_config_db, uid="channel-workflow-user")
        assert store is not None
        assert store.organization_model_id == "organization-chat"

        confirmed = _assert_standard(
            await client.post(
                "/api/v1/channels/update",
                params={"channel_id": channel_id},
                json={
                    "model_ids": [renamed_model],
                    "confirm_config_impact": True,
                    "config_impact_token": impact_token,
                },
            ),
            200,
        )
        assert confirmed["data"]["requires_confirmation"] is False
        assert confirmed["data"]["synced_memory_organization_settings"] == 1
        assert confirmed["data"]["channel"]["model_ids"][0]["model_id"] == "renamed-organization-chat"
        store = await memory_store_crud.get_by_uid(channel_config_db, uid="channel-workflow-user")
        assert store is not None
        assert store.auto_organize_enabled is True
        assert store.organization_channel_id == channel_id
        assert store.organization_model_id == "renamed-organization-chat"

        blocked_delete = _assert_standard(
            await client.post("/api/v1/channels/delete", params={"channel_id": channel_id}),
            400,
        )
        assert blocked_delete["data"] is None
        assert await channel_crud.get(channel_config_db, channel_id) is not None

        disabled = _assert_standard(
            await client.post(
                "/api/v1/memories/settings",
                json={
                    "auto_organize_enabled": False,
                    "organization_channel_id": None,
                    "organization_model_id": None,
                },
            ),
            200,
        )
        assert disabled["data"]["organization"]["auto_organize_enabled"] is False
        assert disabled["data"]["organization"]["model"] is None

        deleted = _assert_standard(
            await client.post("/api/v1/channels/delete", params={"channel_id": channel_id}),
            200,
        )
        assert deleted["data"] == {"removed_profile_rules": 0, "cleared_audit_refs": 0}
        missing = _assert_standard(
            await client.get("/api/v1/channels/get", params={"channel_id": channel_id}),
            404,
        )
        assert missing["data"] is None


@pytest.mark.asyncio
async def test_channel_configuration_persists_reasoning_candidates_and_syncs_profile_references(
    channel_config_app: FastAPI,
    channel_config_db: AsyncSession,
) -> None:
    old_model = _chat_model("candidate-chat")
    old_model["reasoning_efforts"] = [" high ", "", "custom-tier", "high", " manual-tier "]
    expected_old_model = {
        **old_model,
        "advanced_settings": {},
        "reasoning_efforts": ["high", "custom-tier", "manual-tier"],
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=channel_config_app),
        base_url="http://test",
    ) as client:
        created = _assert_standard(
            await client.post(
                "/api/v1/channels/create",
                json={
                    "name": "candidate-channel",
                    "api_key": "candidate-api-key",
                    "base_url": "https://llm.example/v1",
                    "is_active": True,
                    "model_ids": [old_model],
                },
            ),
            200,
        )
        channel_id = created["data"]["id"]
        assert created["data"]["model_ids"] == [expected_old_model]

        first_get = _assert_standard(
            await client.get("/api/v1/channels/get", params={"channel_id": channel_id}),
            200,
        )
        assert first_get["data"]["model_ids"] == [expected_old_model]
        second_get = _assert_standard(
            await client.get("/api/v1/channels/get", params={"channel_id": channel_id}),
            200,
        )
        assert second_get["data"]["model_ids"] == first_get["data"]["model_ids"]
        persisted_channel = await channel_crud.get(channel_config_db, channel_id)
        assert persisted_channel is not None
        assert persisted_channel.model_ids == second_get["data"]["model_ids"]
        assert (await channel_config_db.execute(select(Profile))).scalars().all() == []

        for invalid_efforts in [["high", 123], ["high", "x" * 65]]:
            invalid_model = {**old_model, "reasoning_efforts": invalid_efforts}
            invalid_response = await client.post(
                "/api/v1/channels/update",
                params={"channel_id": channel_id},
                json={"model_ids": [invalid_model]},
            )
            assert invalid_response.status_code == 200
            invalid_payload = invalid_response.json()
            assert invalid_payload["code"] == 422
            assert invalid_payload["data"] is None
            assert old_model["model_id"] in invalid_payload["message"]
            unchanged = _assert_standard(
                await client.get("/api/v1/channels/get", params={"channel_id": channel_id}),
                200,
            )
            assert unchanged["data"]["model_ids"] == [expected_old_model]
            persisted_channel = await channel_crud.get(channel_config_db, channel_id)
            assert persisted_channel is not None
            assert persisted_channel.model_ids == [expected_old_model]

        profile = Profile(
            uid="candidate-profile-user",
            name="candidate-profile",
            configs={
                "channel": {
                    "chat_channel": {
                        "rules": [
                            {
                                "channel_id": channel_id,
                                "model_id": "candidate-chat",
                                "reasoning_effort": "custom-tier",
                                "priority": 1,
                                "weight": 1,
                            }
                        ]
                    }
                },
                "security": {
                    "audit_channel_id": channel_id,
                    "audit_model_id": "candidate-chat",
                },
            },
        )
        channel_config_db.add(profile)
        await channel_config_db.commit()
        await channel_config_db.refresh(profile)
        original_profile_configs = deepcopy(profile.configs)

        renamed_model = {
            **old_model,
            "model_id": "renamed-candidate-chat",
            "reasoning_efforts": [" high ", "custom-tier", "manual-tier "],
        }
        renamed_model_after_repreview = {
            **renamed_model,
            "reasoning_efforts": ["high", "custom-tier", "manual-tier", "experimental-tier"],
        }
        expected_renamed_model = {
            **expected_old_model,
            "model_id": "renamed-candidate-chat",
            "reasoning_efforts": ["high", "custom-tier", "manual-tier", "experimental-tier"],
        }

        preview = _assert_standard(
            await client.post(
                "/api/v1/channels/update",
                params={"channel_id": channel_id},
                json={"model_ids": [renamed_model]},
            ),
            200,
        )
        assert preview["data"]["requires_confirmation"] is True
        assert preview["data"]["synced_profile_rules"] == 1
        assert preview["data"]["synced_audit_refs"] == 1
        assert preview["data"]["removed_profile_rules"] == 0
        assert preview["data"]["cleared_audit_refs"] == 0
        first_token = preview["data"]["config_impact_token"]
        assert isinstance(first_token, str) and len(first_token) == 64

        before_confirm = _assert_standard(
            await client.get("/api/v1/channels/get", params={"channel_id": channel_id}),
            200,
        )
        assert before_confirm["data"]["model_ids"] == [expected_old_model]
        await channel_config_db.refresh(profile)
        assert profile.configs == original_profile_configs

        repreview = _assert_standard(
            await client.post(
                "/api/v1/channels/update",
                params={"channel_id": channel_id},
                json={
                    "model_ids": [renamed_model_after_repreview],
                    "confirm_config_impact": True,
                    "config_impact_token": first_token,
                },
            ),
            200,
        )
        assert repreview["data"]["requires_confirmation"] is True
        assert repreview["data"]["synced_profile_rules"] == 1
        assert repreview["data"]["synced_audit_refs"] == 1
        second_token = repreview["data"]["config_impact_token"]
        assert isinstance(second_token, str) and len(second_token) == 64
        assert second_token != first_token

        still_unchanged = _assert_standard(
            await client.get("/api/v1/channels/get", params={"channel_id": channel_id}),
            200,
        )
        assert still_unchanged["data"]["model_ids"] == [expected_old_model]
        await channel_config_db.refresh(profile)
        assert profile.configs == original_profile_configs

        confirmed = _assert_standard(
            await client.post(
                "/api/v1/channels/update",
                params={"channel_id": channel_id},
                json={
                    "model_ids": [renamed_model_after_repreview],
                    "confirm_config_impact": True,
                    "config_impact_token": second_token,
                },
            ),
            200,
        )
        assert confirmed["data"]["requires_confirmation"] is False
        assert confirmed["data"]["synced_profile_rules"] == 1
        assert confirmed["data"]["synced_audit_refs"] == 1
        assert confirmed["data"]["channel"]["model_ids"] == [expected_renamed_model]
        for field in ("usage", "protocol", "context_window_k", "max_tokens", "temperature", "top_p", "is_enabled", "description"):
            assert confirmed["data"]["channel"]["model_ids"][0][field] == expected_old_model[field]

        await channel_config_db.refresh(profile)
        updated_rule = profile.configs["channel"]["chat_channel"]["rules"][0]
        assert updated_rule["model_id"] == "renamed-candidate-chat"
        assert updated_rule["reasoning_effort"] == "custom-tier"
        assert profile.configs["security"]["audit_channel_id"] == channel_id
        assert profile.configs["security"]["audit_model_id"] == "renamed-candidate-chat"

        delete_preview = _assert_standard(
            await client.post(
                "/api/v1/channels/update",
                params={"channel_id": channel_id},
                json={"model_ids": []},
            ),
            200,
        )
        assert delete_preview["data"]["requires_confirmation"] is True
        assert delete_preview["data"]["removed_profile_rules"] == 1
        assert delete_preview["data"]["cleared_audit_refs"] == 1
        delete_token = delete_preview["data"]["config_impact_token"]
        assert isinstance(delete_token, str) and len(delete_token) == 64

        not_deleted = _assert_standard(
            await client.get("/api/v1/channels/get", params={"channel_id": channel_id}),
            200,
        )
        assert not_deleted["data"]["model_ids"] == [expected_renamed_model]
        await channel_config_db.refresh(profile)
        assert profile.configs["channel"]["chat_channel"]["rules"][0]["model_id"] == "renamed-candidate-chat"
        assert profile.configs["security"]["audit_model_id"] == "renamed-candidate-chat"

        deleted_model = _assert_standard(
            await client.post(
                "/api/v1/channels/update",
                params={"channel_id": channel_id},
                json={
                    "model_ids": [],
                    "confirm_config_impact": True,
                    "config_impact_token": delete_token,
                },
            ),
            200,
        )
        assert deleted_model["data"]["requires_confirmation"] is False
        assert deleted_model["data"]["removed_profile_rules"] == 1
        assert deleted_model["data"]["cleared_audit_refs"] == 1
        assert deleted_model["data"]["channel"]["model_ids"] == []
        await channel_config_db.refresh(profile)
        assert profile.configs["channel"]["chat_channel"]["rules"] == []
        assert profile.configs["security"]["audit_channel_id"] is None
        assert profile.configs["security"]["audit_model_id"] is None
