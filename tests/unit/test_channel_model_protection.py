from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.api.v1 import channels
from app.core.channel_model_protection import find_model_identity_update_conflict
from app.core.constants import ERR_KB_CHANNEL_IN_USE, ERR_KB_MODEL_IDENTITY_IN_USE
from app.core.crud.channel import channel_crud
from app.core.exceptions import ParameterException
from app.models.channel import ChannelCreate, ModelChannel
from app.models.knowledge_base import KnowledgeBase
from app.models.memory import (
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryMutationJob,
    LongTermMemoryStore,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary

PROTECTION_TABLES = [
    ModelChannel.__table__,
    KnowledgeBase.__table__,
    LongTermMemoryStore.__table__,
    LongTermMemoryEmbeddingRevision.__table__,
    LongTermMemoryMutationJob.__table__,
    PromptLibrary.__table__,
    Profile.__table__,
]


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=PROTECTION_TABLES,
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def _embedding_model(model_id: str = "embedding-a", **overrides: object) -> dict:
    model = {
        "model_id": model_id,
        "usage": "EMBEDDING",
        "protocol": "OPENAI_EMBEDDING",
        "embedding_dimensions": 1536,
        "embedding_timeout": 45.0,
        "is_enabled": True,
        "description": "embedding model",
        "advanced_settings": {"custom_headers": {"x-test": "one"}},
    }
    model.update(overrides)
    return model


def _chat_model(model_id: str = "embedding-a") -> dict:
    return {
        "model_id": model_id,
        "usage": "CHAT",
        "protocol": "OPENAI",
        "context_window_k": 128,
        "max_tokens": 4096,
        "is_enabled": True,
        "description": "chat model",
        "advanced_settings": {"custom_headers": {"x-test": "one"}},
    }


async def _create_channel(
    db: AsyncSession,
    *,
    name: str = "protected-channel",
    model_ids: list[dict] | None = None,
) -> ModelChannel:
    return await channel_crud.create(
        db,
        obj_in=ChannelCreate(
            name=name,
            api_key="enc:v1:test-key",
            base_url="https://example.invalid",
            model_ids=model_ids or [_embedding_model()],
        ),
    )


async def _create_knowledge_base(
    db: AsyncSession,
    *,
    channel_id: int,
    model_id: str,
    name: str = "protected-knowledge-base",
    active_channel_id: int | None = None,
    active_model_id: str | None = None,
    target_channel_id: int | None = None,
    target_model_id: str | None = None,
) -> KnowledgeBase:
    knowledge_base = KnowledgeBase(
        uid="knowledge-base-user",
        name=name,
        embedding_channel_id=channel_id,
        embedding_model_id=model_id,
        collection_name=f"collection-{channel_id}-{model_id}",
        active_embedding_channel_id=active_channel_id,
        active_embedding_model_id=active_model_id,
        active_collection_name=(
            f"active-collection-{name}" if active_channel_id is not None else None
        ),
        target_embedding_channel_id=target_channel_id,
        target_embedding_model_id=target_model_id,
        target_collection_name=(
            f"target-collection-{name}" if target_channel_id is not None else None
        ),
    )
    db.add(knowledge_base)
    await db.flush()
    await db.refresh(knowledge_base)
    return knowledge_base


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        pytest.param("remove", id="remove-model"),
        pytest.param("model_id", id="change-model-id"),
        pytest.param("usage", id="change-usage"),
        pytest.param("dimensions", id="change-dimensions"),
    ],
)
async def test_kb_referenced_embedding_model_identity_changes_are_rejected(
    db_session: AsyncSession,
    change: str,
) -> None:
    old_models = [_embedding_model("embedding-a")]
    channel = await _create_channel(db_session, model_ids=old_models)
    channel_id = channel.id
    await _create_knowledge_base(db_session, channel_id=channel_id, model_id="embedding-a")

    if change == "remove":
        new_models = []
    elif change == "model_id":
        new_models = [_embedding_model("embedding-renamed")]
    elif change == "usage":
        new_models = [_chat_model("embedding-a")]
    else:
        new_models = [_embedding_model("embedding-a", embedding_dimensions=3072)]

    with pytest.raises(ParameterException) as exc_info:
        await channels.update_channel(
            channel_id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    assert exc_info.value.message == ERR_KB_MODEL_IDENTITY_IN_USE
    assert exc_info.value.kwargs == {"model_id": "embedding-a"}
    unchanged = await channel_crud.get(db_session, channel_id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models


def test_find_model_identity_update_conflict_covers_protocol_changes() -> None:
    old_models = [_embedding_model("embedding-a")]
    new_models = [_embedding_model("embedding-a", protocol="CHANGED_PROTOCOL")]

    assert (
        find_model_identity_update_conflict(
            {"embedding-a"},
            old_models,
            new_models,
        )
        == "embedding-a"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"is_enabled": False}, id="is-enabled"),
        pytest.param({"description": "updated description"}, id="description"),
        pytest.param({"embedding_timeout": 90.0}, id="embedding-timeout"),
        pytest.param(
            {"advanced_settings": {"custom_headers": {"x-test": "two"}}},
            id="advanced-settings",
        ),
    ],
)
async def test_non_identity_changes_to_kb_referenced_embedding_model_are_allowed(
    db_session: AsyncSession,
    change: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_embedding_model("embedding-a")]
    channel = await _create_channel(db_session, model_ids=old_models)
    await _create_knowledge_base(db_session, channel_id=channel.id, model_id="embedding-a")
    new_models = [{**old_models[0], **change}]

    async def no_op(*_args, **_kwargs) -> int:
        return 0

    monkeypatch.setattr(channels, "_sync_channel_model_id_renames", no_op)
    monkeypatch.setattr(channels, "_sync_audit_model_id_renames", no_op)
    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", no_op)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", no_op)

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models),
        db=db_session,
        admin={},
    )

    assert response.code == 200
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == new_models


@pytest.mark.asyncio
async def test_unreferenced_embedding_model_identity_change_is_allowed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_embedding_model("embedding-a"), _embedding_model("embedding-b")]
    channel = await _create_channel(db_session, model_ids=old_models)
    await _create_knowledge_base(db_session, channel_id=channel.id, model_id="embedding-a")
    new_models = [_embedding_model("embedding-a"), _embedding_model("embedding-b-renamed")]

    async def no_op(*_args, **_kwargs) -> int:
        return 0

    monkeypatch.setattr(channels, "_sync_channel_model_id_renames", no_op)
    monkeypatch.setattr(channels, "_sync_audit_model_id_renames", no_op)
    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", no_op)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", no_op)

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models),
        db=db_session,
        admin={},
    )

    assert response.code == 200
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == new_models


@pytest.mark.asyncio
async def test_kb_channel_reference_rejects_delete_before_profile_and_audit_cleanup(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = await _create_channel(db_session)
    channel_id = channel.id
    await _create_knowledge_base(db_session, channel_id=channel_id, model_id="embedding-a")
    cleanup_calls: list[str] = []

    async def unexpected_cleanup(*_args, **_kwargs) -> int:
        cleanup_calls.append("called")
        return 0

    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", unexpected_cleanup)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", unexpected_cleanup)

    with pytest.raises(ParameterException) as exc_info:
        await channels.delete_channel(channel_id, db=db_session, admin={})

    assert exc_info.value.message == ERR_KB_CHANNEL_IN_USE
    assert cleanup_calls == []
    assert await channel_crud.get(db_session, channel_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["active", "target"])
async def test_kb_active_or_target_channel_reference_rejects_delete(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    reference_kind: str,
) -> None:
    referenced = await _create_channel(
        db_session,
        name=f"referenced-{reference_kind}",
        model_ids=[_embedding_model("embedding-active")],
    )
    legacy = await _create_channel(
        db_session,
        name=f"legacy-{reference_kind}",
        model_ids=[_embedding_model("embedding-legacy")],
    )
    kwargs = (
        {
            "active_channel_id": referenced.id,
            "active_model_id": "embedding-active",
        }
        if reference_kind == "active"
        else {
            "target_channel_id": referenced.id,
            "target_model_id": "embedding-active",
        }
    )
    await _create_knowledge_base(
        db_session,
        channel_id=legacy.id,
        model_id="embedding-legacy",
        name=f"kb-{reference_kind}",
        **kwargs,
    )

    async def unexpected_cleanup(*_args, **_kwargs) -> int:
        raise AssertionError("cleanup must not run before reference protection")

    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", unexpected_cleanup)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", unexpected_cleanup)

    with pytest.raises(ParameterException) as exc_info:
        await channels.delete_channel(referenced.id, db=db_session, admin={})

    assert exc_info.value.message == ERR_KB_CHANNEL_IN_USE


@pytest.mark.asyncio
async def test_kb_partial_active_snapshot_still_protects_legacy_runtime_channel(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = await _create_channel(
        db_session,
        name="partial-active-legacy",
        model_ids=[_embedding_model("embedding-legacy")],
    )
    incomplete_active = await _create_channel(
        db_session,
        name="partial-active-incomplete",
        model_ids=[_embedding_model("embedding-active")],
    )
    knowledge_base = await _create_knowledge_base(
        db_session,
        channel_id=legacy.id,
        model_id="embedding-legacy",
        name="partial-active-kb",
    )
    knowledge_base.active_embedding_channel_id = incomplete_active.id
    knowledge_base.active_embedding_model_id = None
    knowledge_base.active_collection_name = None
    db_session.add(knowledge_base)
    await db_session.commit()

    async def unexpected_cleanup(*_args, **_kwargs) -> int:
        raise AssertionError("legacy runtime reference must be protected")

    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", unexpected_cleanup)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", unexpected_cleanup)

    with pytest.raises(ParameterException) as exc_info:
        await channels.delete_channel(legacy.id, db=db_session, admin={})

    assert exc_info.value.message == ERR_KB_CHANNEL_IN_USE


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["active", "target"])
async def test_kb_active_or_target_embedding_model_identity_is_protected_when_legacy_differs(
    db_session: AsyncSession,
    reference_kind: str,
) -> None:
    active_models = [_embedding_model("embedding-active")]
    active_channel = await _create_channel(
        db_session,
        name="active-channel",
        model_ids=active_models,
    )
    legacy_channel = await _create_channel(
        db_session,
        name="legacy-channel",
        model_ids=[_embedding_model("embedding-legacy")],
    )
    await _create_knowledge_base(
        db_session,
        channel_id=legacy_channel.id,
        model_id="embedding-legacy",
        name=f"{reference_kind}-model-protected",
        **(
            {
                "active_channel_id": active_channel.id,
                "active_model_id": "embedding-active",
            }
            if reference_kind == "active"
            else {
                "target_channel_id": active_channel.id,
                "target_model_id": "embedding-active",
            }
        ),
    )

    with pytest.raises(ParameterException) as exc_info:
        await channels.update_channel(
            active_channel.id,
            channels.ChannelUpdate(model_ids=[]),
            db=db_session,
            admin={},
        )

    assert exc_info.value.message == ERR_KB_MODEL_IDENTITY_IN_USE
    assert exc_info.value.kwargs == {"model_id": "embedding-active"}


@pytest.mark.asyncio
async def test_unreferenced_channel_delete_runs_mocked_cleanup(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = await _create_channel(db_session)
    cleanup_calls: list[tuple[str, int, list[dict]]] = []

    async def remove_rules(_db, channel_id: int, model_ids: list[dict]) -> int:
        cleanup_calls.append(("profile", channel_id, model_ids))
        return 1

    async def clear_audit(_db, channel_id: int, model_ids: list[dict]) -> int:
        cleanup_calls.append(("audit", channel_id, model_ids))
        return 1

    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", remove_rules)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", clear_audit)

    response = await channels.delete_channel(channel.id, db=db_session, admin={})

    assert response.code == 200
    assert cleanup_calls == [
        ("profile", channel.id, []),
        ("audit", channel.id, []),
    ]
    assert await channel_crud.get(db_session, channel.id) is None


@pytest.mark.asyncio
async def test_kb_identity_protection_runs_before_all_update_cleanup_and_sync(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_embedding_model("embedding-a"), _chat_model("chat-a")]
    channel = await _create_channel(db_session, model_ids=old_models)
    channel_id = channel.id
    await _create_knowledge_base(db_session, channel_id=channel_id, model_id="embedding-a")
    cleanup_calls: list[str] = []

    async def unexpected_sync_or_cleanup(*_args, **_kwargs) -> int:
        cleanup_calls.append("called")
        return 0

    monkeypatch.setattr(channels, "_sync_channel_model_id_renames", unexpected_sync_or_cleanup)
    monkeypatch.setattr(channels, "_sync_audit_model_id_renames", unexpected_sync_or_cleanup)
    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", unexpected_sync_or_cleanup)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", unexpected_sync_or_cleanup)

    with pytest.raises(ParameterException) as exc_info:
        await channels.update_channel(
            channel_id,
            channels.ChannelUpdate(model_ids=[_chat_model("chat-a")]),
            db=db_session,
            admin={},
        )

    assert exc_info.value.message == ERR_KB_MODEL_IDENTITY_IN_USE
    assert exc_info.value.kwargs == {"model_id": "embedding-a"}
    assert cleanup_calls == []
    unchanged = await channel_crud.get(db_session, channel_id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models
