from collections.abc import AsyncGenerator
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.api.v1.profile import router
from app.core import memory_embedding_config as embedding_service
from app.core.crud.profile import profile_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseProfileBinding
from app.models.memory import (
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryEmbeddingSelectionToken,
    LongTermMemoryMutationJob,
    LongTermMemoryRecord,
    LongTermMemoryStore,
)
from app.models.profile import (
    LongTermMemoryConfig,
    Profile,
    ProfileConfig,
    ProfileMemoryEmbeddingConfirmRequest,
    ProfileMemoryEmbeddingPreviewRequest,
)
from app.providers.database import get_db

API_TABLES = [
    Profile.__table__,
    KnowledgeBase.__table__,
    KnowledgeBaseProfileBinding.__table__,
    LongTermMemoryStore.__table__,
    LongTermMemoryEmbeddingSelectionToken.__table__,
    LongTermMemoryEmbeddingRevision.__table__,
    LongTermMemoryMutationJob.__table__,
    LongTermMemoryRecord.__table__,
]


def _memory_config(**overrides: object) -> LongTermMemoryConfig:
    values = {
        "enabled": False,
        "embedding_channel_id": None,
        "embedding_model_id": None,
        "top_k": 5,
        "candidate_k": 10,
        "result_max_chars": 4000,
    }
    values.update(overrides)
    return LongTermMemoryConfig.model_validate(values)


def _profile_configs(memory: LongTermMemoryConfig | None = None) -> dict:
    return ProfileConfig.model_validate({"memory": (memory or _memory_config()).model_dump()}).model_dump()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=API_TABLES))

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def api_app(db_session: AsyncSession, monkeypatch) -> tuple[FastAPI, SimpleNamespace]:
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")
    current_user = SimpleNamespace(uid="user-a", is_superuser=False)

    async def override_get_db():
        yield db_session

    def override_get_current_user():
        return current_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    async def fake_load(_db, channel_id: int, model_id: str) -> EmbeddingRuntimeConfig:
        return EmbeddingRuntimeConfig(
            channel_id=channel_id,
            channel_name=f"channel-{channel_id}",
            model_id=model_id,
            declared_dimensions=1536,
            protocol="openai_embedding",
            timeout=30.0,
            base_url="https://embedding.invalid/v1",
            api_key="test-api-key",
        )

    async def fake_detect(_config: EmbeddingRuntimeConfig) -> int:
        return 3

    monkeypatch.setattr(embedding_service, "load_embedding_runtime_config", fake_load)
    monkeypatch.setattr(embedding_service, "detect_embedding_dimensions", fake_detect)
    return app, current_user


@pytest.mark.asyncio
async def test_profile_memory_embedding_api_success_replay_and_standard_response(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, _current_user = api_app
    profile = await profile_crud.create(
        db_session,
        obj_in={"uid": "user-a", "name": "profile-a", "configs": _profile_configs()},
    )
    profile_id = profile.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        preview_response = await client.post(
            "/api/v1/profiles/memory-embedding-preview",
            json={"profile_id": profile_id, "embedding_channel_id": 7, "embedding_model_id": "embed-v1"},
        )
        preview_payload = preview_response.json()
        assert preview_response.status_code == 200
        assert preview_payload["code"] == 200
        assert set(preview_payload) == {"code", "message", "data"}
        assert preview_payload["data"]["dimensions"] == 3
        assert preview_payload["data"]["actual_dimensions"] == 3
        assert "embedding_selection_signature" in preview_payload["data"]
        assert {key for key in preview_payload["data"] if "signature" in key} == {"embedding_selection_signature"}
        assert "embedding_selection_token" not in preview_payload["data"]
        assert "token" not in preview_payload["data"]
        token = preview_payload["data"]["embedding_selection_signature"]

        confirm_response = await client.post(
            "/api/v1/profiles/memory-embedding-confirm",
            json={
                "profile_id": profile_id,
                "memory": {"enabled": True, "top_k": 6, "candidate_k": 9, "result_max_chars": 5000},
                "embedding_selection_signature": token,
            },
        )
        confirm_payload = confirm_response.json()
        assert confirm_response.status_code == 200
        assert confirm_payload["code"] == 200
        assert set(confirm_payload) == {"code", "message", "data"}
        assert confirm_payload["data"]["configs"]["memory"]["embedding_channel_id"] == 7
        assert confirm_payload["data"]["configs"]["memory"]["embedding_model_id"] == "embed-v1"
        assert confirm_payload["data"]["configs"]["memory"]["enabled"] is True
        assert confirm_payload["data"]["memory_runtime"]["embedding_channel_id"] == 7
        assert confirm_payload["data"]["memory_runtime"]["embedding_model_id"] == "embed-v1"
        assert confirm_payload["data"]["memory_runtime"]["embedding_dimensions"] == 3
        assert confirm_payload["data"]["memory_runtime"]["embedding_revision"] == 1
        assert confirm_payload["data"]["memory_runtime"]["migration_job_id"] is None

        replay_response = await client.post(
            "/api/v1/profiles/memory-embedding-confirm",
            json={
                "profile_id": profile_id,
                "memory": {"enabled": True, "top_k": 6, "candidate_k": 9, "result_max_chars": 5000},
                "embedding_selection_signature": token,
            },
        )
        replay_payload = replay_response.json()

    assert replay_response.status_code == 400
    assert replay_payload["code"] == 400
    assert set(replay_payload) == {"code", "message", "data"}
    assert replay_payload["data"] is None


def test_profile_memory_embedding_request_protocol_has_no_token_alias_or_client_dimension_fields() -> None:
    preview_fields = set(ProfileMemoryEmbeddingPreviewRequest.model_fields)
    confirm_fields = set(ProfileMemoryEmbeddingConfirmRequest.model_fields)
    memory_fields = set(LongTermMemoryConfig.model_fields)

    assert "embedding_selection_token" not in preview_fields | confirm_fields | memory_fields
    assert "token" not in preview_fields | confirm_fields | memory_fields
    assert preview_fields == {"profile_id", "embedding_channel_id", "embedding_model_id"}
    assert confirm_fields == {"profile_id", "memory", "embedding_selection_signature"}
    assert "embedding_selection_signature" not in memory_fields
    assert "embedding_dimensions" not in preview_fields | confirm_fields | memory_fields
    assert "dimensions" not in preview_fields | confirm_fields | memory_fields
    assert "embedding_signature" not in preview_fields | confirm_fields | memory_fields


@pytest.mark.asyncio
async def test_profile_memory_embedding_preview_probe_failure_returns_standard_response_without_token(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _current_user = api_app
    profile = await profile_crud.create(
        db_session,
        obj_in={"uid": "user-a", "name": "profile-a", "configs": _profile_configs()},
    )
    profile_id = profile.id

    async def fail_detect(_config: EmbeddingRuntimeConfig) -> int:
        raise RuntimeError("probe failed")

    monkeypatch.setattr(embedding_service, "detect_embedding_dimensions", fail_detect)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/profiles/memory-embedding-preview",
            json={"profile_id": profile_id, "embedding_channel_id": 7, "embedding_model_id": "embed-v1"},
        )

    payload = response.json()
    assert response.status_code == 502
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 502
    assert isinstance(payload["message"], str)
    assert payload["message"]
    assert payload["data"] is None
    assert "choices" not in payload
    selections = list((await db_session.execute(select(LongTermMemoryEmbeddingSelectionToken))).scalars().all())
    assert selections == []


@pytest.mark.asyncio
async def test_profile_memory_embedding_api_rejects_other_user_preview_and_confirm_with_standard_response(
    api_app: tuple[FastAPI, SimpleNamespace],
    db_session: AsyncSession,
) -> None:
    app, current_user = api_app
    profile = await profile_crud.create(
        db_session,
        obj_in={"uid": "user-a", "name": "profile-a", "configs": _profile_configs()},
    )
    other_profile = await profile_crud.create(
        db_session,
        obj_in={"uid": "user-b", "name": "profile-b", "configs": _profile_configs()},
    )
    profile_id = profile.id
    other_profile_uid = other_profile.uid

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        preview_response = await client.post(
            "/api/v1/profiles/memory-embedding-preview",
            json={"profile_id": profile_id, "embedding_channel_id": 7, "embedding_model_id": "embed-v1"},
        )
        assert preview_response.status_code == 200
        token = preview_response.json()["data"]["embedding_selection_signature"]

        current_user.uid = "user-b"
        forbidden_preview = await client.post(
            "/api/v1/profiles/memory-embedding-preview",
            json={"profile_id": profile_id, "embedding_channel_id": 7, "embedding_model_id": "embed-v1"},
        )
        forbidden_confirm = await client.post(
            "/api/v1/profiles/memory-embedding-confirm",
            json={
                "profile_id": profile_id,
                "memory": {"enabled": True},
                "embedding_selection_signature": token,
            },
        )

    assert other_profile_uid == "user-b"
    for response in (forbidden_preview, forbidden_confirm):
        payload = response.json()
        assert response.status_code == 404
        assert payload["code"] == 404
        assert set(payload) == {"code", "message", "data"}
        assert payload["data"] is None

    selection = (
        (
            await db_session.execute(
                select(LongTermMemoryEmbeddingSelectionToken).where(
                    LongTermMemoryEmbeddingSelectionToken.uid == "user-a",
                    LongTermMemoryEmbeddingSelectionToken.profile_id == profile_id,
                )
            )
        )
        .scalars()
        .one()
    )
    assert selection.consumed_at is None
