import json
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import app.core.crypto as crypto_module
from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED,
    ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID,
    ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    MEMORY_CONTENT_MAX_TOKENS,
    MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
    MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS,
    MEMORY_ORGANIZE_POLICY_VERSION,
)
from app.core.crud.channel import channel_crud
from app.core.crud.memory import memory_store_crud
from app.core.exceptions import ParameterException
from app.core.memory.organization import (
    calculate_organization_required_output_tokens,
    get_organization_settings,
    load_organization_model_config,
    update_organization_settings,
)
from app.models.channel import ChannelCreate, ModelChannel
from app.models.memory import LongTermMemoryRecord, LongTermMemoryStore

ORGANIZATION_TABLES = [
    ModelChannel.__table__,
    LongTermMemoryStore.__table__,
    LongTermMemoryRecord.__table__,
]


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"\x00" * 32)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=ORGANIZATION_TABLES,
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def _chat_model(model_id: str = "chat-model", **overrides: object) -> dict:
    model = {
        "model_id": model_id,
        "usage": "CHAT",
        "protocol": "OPENAI",
        "context_window_k": 32,
        "max_tokens": 2048,
        "temperature": 0.25,
        "top_p": 0.8,
        "is_enabled": True,
        "description": "organization model",
        "advanced_settings": {"custom_headers": {"x-trace": "organization"}},
    }
    model.update(overrides)
    return model


async def _create_channel(
    db: AsyncSession,
    *,
    name: str = "organization-channel",
    api_key: str = "organization-api-key",
    base_url: str | None = "https://llm.example/v1",
    http_proxy: str | None = "http://proxy.example:8080",
    is_active: bool = True,
    model_ids: list[dict] | None = None,
    encrypted_api_key: bool = True,
) -> ModelChannel:
    channel_in = ChannelCreate(
        name=name,
        api_key=api_key,
        base_url=base_url,
        http_proxy=http_proxy,
        is_active=is_active,
        model_ids=model_ids or [_chat_model()],
    )
    if encrypted_api_key:
        return await channel_crud.create_with_plain_api_key(db, obj_in=channel_in)
    return await channel_crud.create(db, obj_in=channel_in)


async def _create_store(
    db: AsyncSession,
    *,
    uid: str,
    channel_id: int | None = None,
    model_id: str | None = None,
    auto_organize_enabled: bool = False,
    **values: object,
) -> LongTermMemoryStore:
    return await memory_store_crud.create(
        db,
        uid=uid,
        organization_channel_id=channel_id,
        organization_model_id=model_id,
        auto_organize_enabled=auto_organize_enabled,
        **values,
    )


@pytest.mark.parametrize(
    ("snapshot_count", "expected"),
    [
        (0, 0),
        (1, MEMORY_CONTENT_MAX_TOKENS + MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS),
        (45, 45 * (MEMORY_CONTENT_MAX_TOKENS + MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS)),
        (50, 50 * (MEMORY_CONTENT_MAX_TOKENS + MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS)),
    ],
)
def test_calculate_organization_required_output_tokens_formula(snapshot_count: int, expected: int) -> None:
    assert calculate_organization_required_output_tokens(snapshot_count) == expected


@pytest.mark.parametrize("snapshot_count", [True, False, 1.0, "1", -1])
def test_calculate_organization_required_output_tokens_rejects_invalid_values(snapshot_count: object) -> None:
    with pytest.raises(ParameterException) as exc_info:
        calculate_organization_required_output_tokens(snapshot_count)  # type: ignore[arg-type]

    if snapshot_count == -1:
        assert exc_info.value.message == ERR_VALUE_MUST_BE_NON_NEGATIVE
    else:
        assert exc_info.value.message == ERR_MEMORY_FIELD_TYPE_INVALID
    assert exc_info.value.kwargs == {"field": "snapshot_count"}


@pytest.mark.asyncio
async def test_load_valid_chat_organization_config_and_runtime_snapshot(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(db_session)
    await _create_store(
        db_session,
        uid="organization-user",
        channel_id=channel.id,
        model_id="chat-model",
        organization_policy_version=7,
    )

    config = await load_organization_model_config(
        db_session,
        uid="organization-user",
        snapshot_count=2,
    )

    assert config.channel_id == channel.id
    assert config.channel_name == "organization-channel"
    assert config.model_id == "chat-model"
    assert config.usage == "CHAT"
    assert config.protocol == "openai"
    assert config.context_window_k == 32
    assert config.context_window_tokens == 32 * CONTEXT_WINDOW_TOKENS_PER_K
    assert config.max_tokens == 2048
    assert config.snapshot_count == 2
    assert config.required_output_tokens == 2 * (MEMORY_CONTENT_MAX_TOKENS + MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS)
    assert config.temperature == 0.25
    assert config.top_p == 0.8
    assert config.timeout == MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS
    assert config.api_key == "organization-api-key"
    assert config.base_url == "https://llm.example/v1"
    assert config.http_proxy == "http://proxy.example:8080"
    assert dict(config.custom_headers) == {"x-trace": "organization"}

    with pytest.raises(TypeError):
        config.custom_headers["x-new"] = "value"  # type: ignore[index]

    snapshot = config.to_job_snapshot()
    json.dumps(snapshot)
    assert snapshot == {
        "channel_id": channel.id,
        "channel_name": "organization-channel",
        "model_id": "chat-model",
        "usage": "CHAT",
        "protocol": "openai",
        "base_url": "https://llm.example/v1",
        "api_key": "organization-api-key",
        "http_proxy": "http://proxy.example:8080",
        "custom_headers": {"x-trace": "organization"},
        "temperature": 0.25,
        "top_p": 0.8,
        "timeout": MEMORY_ORGANIZE_LLM_TIMEOUT_SECONDS,
        "context_window_k": 32,
        "context_window_tokens": 32 * CONTEXT_WINDOW_TOKENS_PER_K,
        "max_tokens": 2048,
        "snapshot_count": 2,
        "required_output_tokens": 2 * (MEMORY_CONTENT_MAX_TOKENS + MEMORY_ORGANIZE_OUTPUT_ITEM_OVERHEAD_TOKENS),
        "policy_version": 7,
    }

    public = config.to_public_dict()
    assert "base_url" not in public
    assert "api_key" not in public
    assert "http_proxy" not in public
    assert "custom_headers" not in public
    assert public["model_id"] == "chat-model"
    assert public["max_tokens"] == 2048
    assert public["required_output_tokens"] == snapshot["required_output_tokens"]

    channel.model_ids = [_chat_model(temperature=1.5, max_tokens=4096)]
    channel.base_url = "https://changed.example"
    channel.http_proxy = "http://changed.example:8081"
    db_session.add(channel)
    await db_session.flush()

    assert config.temperature == 0.25
    assert config.max_tokens == 2048
    assert config.base_url == "https://llm.example/v1"
    assert config.http_proxy == "http://proxy.example:8080"
    assert config.to_job_snapshot() == snapshot


@pytest.mark.asyncio
async def test_organization_model_config_rejects_output_budget_below_required_tokens(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(
        db_session,
        model_ids=[_chat_model(max_tokens=255)],
    )
    await _create_store(
        db_session,
        uid="context-user",
        channel_id=channel.id,
        model_id="chat-model",
    )

    with pytest.raises(ParameterException) as exc_info:
        await load_organization_model_config(db_session, uid="context-user", snapshot_count=1)

    assert exc_info.value.message == ERR_MEMORY_ORGANIZATION_CONTEXT_EXCEEDED
    assert exc_info.value.kwargs == {"required_tokens": 256, "available_tokens": 255}
    assert exc_info.value.data == {"required_tokens": 256, "available_tokens": 255}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "channel_missing",
        "channel_disabled",
        "url_invalid",
        "api_key_undecryptable",
        "usage_not_chat",
        "model_disabled",
        "protocol_invalid",
        "context_missing",
        "context_non_positive",
        "max_tokens_missing",
        "max_tokens_non_positive",
        "model_id_whitespace",
    ],
)
async def test_invalid_organization_model_config_is_rejected(
    db_session: AsyncSession,
    case: str,
) -> None:
    model_id = "chat-model"
    channel_id: int | None = 999
    model = _chat_model()
    channel_values: dict[str, object] = {"name": f"invalid-{case}"}

    if case == "channel_disabled":
        channel_values["is_active"] = False
    elif case == "url_invalid":
        channel_values["base_url"] = "not-a-url"
    elif case == "api_key_undecryptable":
        channel_values["api_key"] = "enc:v1:not-base64"
        channel_values["encrypted_api_key"] = False
    elif case == "usage_not_chat":
        model = {
            "model_id": model_id,
            "usage": "EMBEDDING",
            "protocol": "OPENAI_EMBEDDING",
            "embedding_dimensions": 1536,
            "is_enabled": True,
        }
    elif case == "model_disabled":
        model["is_enabled"] = False
    elif case == "protocol_invalid":
        model["protocol"] = "INVALID_PROTOCOL"
    elif case == "context_missing":
        model.pop("context_window_k")
    elif case == "context_non_positive":
        model["context_window_k"] = 0
    elif case == "max_tokens_missing":
        model.pop("max_tokens")
    elif case == "max_tokens_non_positive":
        model["max_tokens"] = 0
    elif case == "model_id_whitespace":
        model_id = " chat-model "

    if case != "channel_missing":
        channel = await _create_channel(
            db_session,
            model_ids=[model],
            **channel_values,
        )
        channel_id = channel.id

    await _create_store(
        db_session,
        uid=f"invalid-{case}",
        channel_id=channel_id,
        model_id=model_id,
    )

    with pytest.raises(ParameterException) as exc_info:
        await load_organization_model_config(
            db_session,
            uid=f"invalid-{case}",
            snapshot_count=0,
        )

    assert exc_info.value.message == ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID


@pytest.mark.asyncio
async def test_unconfigured_organization_model_uses_not_configured_error(
    db_session: AsyncSession,
) -> None:
    await _create_store(db_session, uid="unconfigured-user")

    with pytest.raises(ParameterException) as exc_info:
        await load_organization_model_config(db_session, uid="unconfigured-user", snapshot_count=0)

    assert exc_info.value.message == ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED


@pytest.mark.asyncio
async def test_update_organization_settings_writes_store_and_exposes_only_public_model_data(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(db_session)
    await _create_store(db_session, uid="settings-user")

    result = await update_organization_settings(
        db_session,
        uid="settings-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="chat-model",
    )

    store = await memory_store_crud.get_snapshot_by_uid(db_session, uid="settings-user")
    assert store is not None
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "chat-model"
    assert result["auto_organize_enabled"] is True
    assert result["channel_id"] == channel.id
    assert result["model_id"] == "chat-model"
    assert result["model"] is not None
    assert result["model"]["model_id"] == "chat-model"
    assert "base_url" not in result["model"]
    assert "api_key" not in result["model"]
    assert "http_proxy" not in result["model"]
    assert "custom_headers" not in result["model"]


@pytest.mark.asyncio
async def test_enabling_without_model_rejects_without_half_update_and_disabling_clears_store(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(db_session, name="settings-existing-channel")
    channel_id = channel.id
    await _create_store(
        db_session,
        uid="settings-transition-user",
        channel_id=channel_id,
        model_id="chat-model",
        auto_organize_enabled=False,
    )

    with pytest.raises(ParameterException) as exc_info:
        await update_organization_settings(
            db_session,
            uid="settings-transition-user",
            auto_organize_enabled=True,
            organization_channel_id=None,
            organization_model_id=None,
        )

    assert exc_info.value.message == ERR_MEMORY_ORGANIZATION_MODEL_NOT_CONFIGURED
    unchanged = await memory_store_crud.get_snapshot_by_uid(db_session, uid="settings-transition-user")
    assert unchanged is not None
    assert unchanged.auto_organize_enabled is False
    assert unchanged.organization_channel_id == channel_id
    assert unchanged.organization_model_id == "chat-model"

    result = await update_organization_settings(
        db_session,
        uid="settings-transition-user",
        auto_organize_enabled=False,
        organization_channel_id=None,
        organization_model_id=None,
    )
    cleared = await memory_store_crud.get_snapshot_by_uid(db_session, uid="settings-transition-user")
    assert cleared is not None
    assert cleared.auto_organize_enabled is False
    assert cleared.organization_channel_id is None
    assert cleared.organization_model_id is None
    assert result["model"] is None


@pytest.mark.asyncio
async def test_organization_settings_without_store_defaults_to_disabled_and_no_model(
    db_session: AsyncSession,
) -> None:
    result = await get_organization_settings(
        db_session,
        uid="missing-settings-user",
        snapshot_count=0,
    )

    assert result == {
        "auto_organize_enabled": False,
        "channel_id": None,
        "model_id": None,
        "policy_version": MEMORY_ORGANIZE_POLICY_VERSION,
        "last_job_id": None,
        "last_run_at": None,
        "error": None,
        "snapshot_count": 0,
        "required_output_tokens": 0,
        "model": None,
        "validation_error": None,
    }
