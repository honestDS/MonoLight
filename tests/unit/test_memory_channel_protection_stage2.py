from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.api.v1 import channels
from app.core import crypto as crypto_module
from app.core.channel_model_protection import (
    assert_channel_model_identity_update_allowed,
    assert_channel_not_referenced,
    finalize_pending_channel_model_deletions_for_organization_job,
)
from app.core.constants import (
    ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS,
    ERR_CHANNEL_MODEL_LOCKED,
    ERR_MEMORY_CHANNEL_IN_USE,
    ERR_MEMORY_MODEL_IDENTITY_IN_USE,
)
from app.core.crud.channel import channel_crud
from app.core.crud.memory import (
    memory_embedding_revision_crud,
    memory_record_crud,
    memory_store_crud,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.memory.channel_protection import list_memory_channel_references
from app.core.memory.organization import (
    build_organization_snapshot_items,
    calculate_organization_required_input_tokens,
)
from app.models.channel import ChannelCreate, ModelChannel, normalize_channel_model_ids
from app.models.knowledge_base import KnowledgeBase
from app.models.memory import (
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryOldCollectionCleanupStatus,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryStore,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary

MEMORY_TABLES = [
    PromptLibrary.__table__,
    Profile.__table__,
    ModelChannel.__table__,
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
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=MEMORY_TABLES,
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            session.info["sync_engine"] = engine.sync_engine
            yield session
    finally:
        await engine.dispose()


def _embedding_model(model_id: str = "embedding-used", **overrides: object) -> dict:
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


def _chat_model(model_id: str = "chat-model", **overrides: object) -> dict:
    model = {
        "model_id": model_id,
        "usage": "CHAT",
        "protocol": "OPENAI",
        "context_window_k": 128,
        "max_tokens": 4096,
        "is_enabled": True,
    }
    model.update(overrides)
    return model


def _organization_chat_model(model_id: str = "organization-chat", **overrides: object) -> dict:
    model = {
        "model_id": model_id,
        "usage": "CHAT",
        "protocol": "OPENAI",
        "image_understanding": False,
        "audio_understanding": False,
        "video_understanding": False,
        "context_window_k": 128,
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 4096,
        "is_enabled": True,
        "description": "organization model",
        "advanced_settings": {"custom_headers": {"x-test": "one"}},
    }
    model.update(overrides)
    return model


@contextmanager
def _capture_writes(db: AsyncSession) -> Iterator[list[str]]:
    writes: list[str] = []
    sync_engine = db.info["sync_engine"]

    def before_cursor_execute(_connection, _cursor, statement, _parameters, _context, _executemany):
        if isinstance(statement, str) and statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield writes
    finally:
        event.remove(sync_engine, "before_cursor_execute", before_cursor_execute)


def _assert_only_channel_lock_writes(writes: list[str]) -> None:
    for statement in writes:
        normalized = " ".join(statement.strip().upper().split())
        assert normalized.startswith("UPDATE CHANNEL SET ID=CHANNEL.ID")


def _patch_profile_sync_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_op(*_args, **_kwargs) -> int:
        return 0

    monkeypatch.setattr(channels, "_sync_channel_model_id_renames", no_op)
    monkeypatch.setattr(channels, "_sync_audit_model_id_renames", no_op)
    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", no_op)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", no_op)


def _assert_update_impact(
    response,
    *,
    requires_confirmation: bool,
    synced: int = 0,
    retained: int = 0,
    disabled: int = 0,
    concurrently_disabled: int = 0,
    deferred: int = 0,
    pending_deletion_models: int = 0,
) -> None:
    assert response.code == 200
    assert response.data["requires_confirmation"] is requires_confirmation
    assert response.data["synced_memory_organization_settings"] == synced
    assert response.data["retained_memory_organization_settings"] == retained
    assert response.data["disabled_memory_organization_settings"] == disabled
    assert response.data["deferred_memory_organization_settings"] == deferred
    assert response.data["concurrently_disabled_memory_organization_settings"] == concurrently_disabled
    assert response.data["pending_deletion_models"] == pending_deletion_models
    assert response.data["synced_profile_rules"] == 0
    assert response.data["removed_profile_rules"] == 0
    assert response.data["synced_audit_refs"] == 0
    assert response.data["cleared_audit_refs"] == 0


async def _create_active_memory_record(db: AsyncSession, *, uid: str, memory_key: str) -> LongTermMemoryRecord:
    return await memory_record_crud.create(
        db,
        uid=uid,
        memory_key=memory_key,
        content="active memory",
        content_token_count=2,
        content_hash=f"{uid}:{memory_key}",
        version=1,
        indexed_version=1,
        vector_item_id=f"{uid}:{memory_key}",
        index_status=LongTermMemoryRecordIndexStatus.READY,
        is_active=True,
        commit=False,
    )


async def _create_store(
    db: AsyncSession,
    *,
    uid: str,
    active_channel_id: int | None = None,
    active_model_id: str | None = None,
    target_channel_id: int | None = None,
    target_model_id: str | None = None,
    **values: object,
) -> LongTermMemoryStore:
    store_values = {
        "active_embedding_channel_id": active_channel_id,
        "active_embedding_model_id": active_model_id,
        "target_embedding_channel_id": target_channel_id,
        "target_embedding_model_id": target_model_id,
        **values,
    }
    return await memory_store_crud.create(db, uid=uid, commit=False, **store_values)


async def _create_embedding_migration_job(
    db: AsyncSession,
    *,
    uid: str,
    dedupe_key: str,
    status: LongTermMemoryMutationStatus,
    payload: dict,
) -> LongTermMemoryMutationJob:
    job, created = await memory_job_crud.create(
        db,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        dedupe_key=dedupe_key,
        status=status,
        payload=payload,
        commit=False,
    )
    assert created
    return job


async def _create_organization_job(
    db: AsyncSession,
    *,
    uid: str,
    dedupe_key: str,
    status: LongTermMemoryMutationStatus,
    channel_id: int,
    model_id: str,
) -> LongTermMemoryMutationJob:
    job, created = await memory_job_crud.create(
        db,
        uid=uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE,
        dedupe_key=dedupe_key,
        status=status,
        payload={
            "model_config": {
                "channel_id": channel_id,
                "model_id": model_id,
                "protocol": "openai",
                "max_tokens": 2048,
            }
        },
        commit=False,
    )
    assert created
    return job


async def _references(db: AsyncSession, channel_id: int) -> set[tuple[str, int, str | None]]:
    references = await list_memory_channel_references(db, channel_id=channel_id)
    return {(reference.uid, reference.channel_id, reference.model_id) for reference in references}


async def _create_channel(
    db: AsyncSession,
    *,
    name: str,
    model_ids: list[dict] | None = None,
) -> ModelChannel:
    return await channel_crud.create_with_plain_api_key(
        db,
        obj_in=ChannelCreate(
            name=name,
            api_key="test-key",
            base_url="https://example.invalid",
            model_ids=model_ids or [],
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["active", "target"])
async def test_active_or_target_store_reference_protects_channel(
    db_session: AsyncSession,
    reference_kind: str,
) -> None:
    values = {
        "active_channel_id": 101 if reference_kind == "active" else None,
        "active_model_id": "active-model" if reference_kind == "active" else None,
        "target_channel_id": 101 if reference_kind == "target" else None,
        "target_model_id": "target-model" if reference_kind == "target" else None,
    }
    await _create_store(db_session, uid="store-user", **values)

    references = await _references(db_session, 101)

    assert references == {
        (
            "store-user",
            101,
            "active-model" if reference_kind == "active" else "target-model",
        )
    }
    with pytest.raises(ParameterException) as exc_info:
        await assert_channel_not_referenced(db_session, channel_id=101)
    assert exc_info.value.message == ERR_MEMORY_CHANNEL_IN_USE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        LongTermMemoryMutationStatus.PENDING,
        LongTermMemoryMutationStatus.RUNNING,
        LongTermMemoryMutationStatus.RETRY,
        LongTermMemoryMutationStatus.FAILED,
        LongTermMemoryMutationStatus.SUCCEEDED,
        LongTermMemoryMutationStatus.CANCELLED,
    ],
)
async def test_embedding_migration_job_payload_protection_depends_on_retryable_status(
    db_session: AsyncSession,
    status: LongTermMemoryMutationStatus,
) -> None:
    await _create_embedding_migration_job(
        db_session,
        uid="job-user",
        dedupe_key=f"migration-{status.value}",
        status=status,
        payload={
            "from": {"channel_id": 102, "model_id": "old-embedding"},
            "target": {"channel_id": 103, "model_id": "new-embedding"},
        },
    )

    if status in {
        LongTermMemoryMutationStatus.PENDING,
        LongTermMemoryMutationStatus.RUNNING,
        LongTermMemoryMutationStatus.RETRY,
        LongTermMemoryMutationStatus.FAILED,
    }:
        assert await _references(db_session, 102) == {("job-user", 102, "old-embedding")}
        assert await _references(db_session, 103) == {("job-user", 103, "new-embedding")}
        for channel_id in (102, 103):
            with pytest.raises(ParameterException) as exc_info:
                await assert_channel_not_referenced(db_session, channel_id=channel_id)
            assert exc_info.value.message == ERR_MEMORY_CHANNEL_IN_USE
    else:
        assert await _references(db_session, 102) == set()
        assert await _references(db_session, 103) == set()
        await assert_channel_not_referenced(db_session, channel_id=102)
        await assert_channel_not_referenced(db_session, channel_id=103)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_status",
    [
        LongTermMemoryOldCollectionCleanupStatus.PENDING,
        LongTermMemoryOldCollectionCleanupStatus.RUNNING,
        LongTermMemoryOldCollectionCleanupStatus.FAILED,
    ],
)
async def test_active_old_collection_cleanup_protects_revision_channels_and_models(
    db_session: AsyncSession,
    cleanup_status: LongTermMemoryOldCollectionCleanupStatus,
) -> None:
    await _create_store(
        db_session,
        uid="revision-user",
        old_collection_name="old-collection",
        old_collection_cleanup_status=cleanup_status,
    )
    await memory_embedding_revision_crud.create(
        db_session,
        uid="revision-user",
        revision=1,
        from_channel_id=104,
        from_model_id="old-model",
        from_dimensions=1536,
        from_collection="old-collection",
        to_channel_id=105,
        to_model_id="new-model",
        to_dimensions=3072,
        to_collection="new-collection",
        commit=False,
    )

    references = await _references(db_session, 104)

    assert references == {("revision-user", 104, "old-model")}
    with pytest.raises(ParameterException) as exc_info:
        await assert_channel_not_referenced(db_session, channel_id=104)
    assert exc_info.value.message == ERR_MEMORY_CHANNEL_IN_USE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_status",
    [
        LongTermMemoryOldCollectionCleanupStatus.PENDING,
        LongTermMemoryOldCollectionCleanupStatus.RUNNING,
        LongTermMemoryOldCollectionCleanupStatus.FAILED,
    ],
)
async def test_active_old_collection_cleanup_protects_cleanup_job_payload(
    db_session: AsyncSession,
    cleanup_status: LongTermMemoryOldCollectionCleanupStatus,
) -> None:
    job, created = await memory_job_crud.create(
        db_session,
        uid="cleanup-user",
        operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
        dedupe_key=f"cleanup-{cleanup_status.value}",
        status=LongTermMemoryMutationStatus.SUCCEEDED,
        payload={"old": {"channel_id": 106, "model_id": "old-cleanup-model"}},
        commit=False,
    )
    assert created
    await _create_store(
        db_session,
        uid="cleanup-user",
        old_collection_name="old-collection",
        old_collection_cleanup_status=cleanup_status,
        old_collection_cleanup_job_id=job.id,
    )

    references = await _references(db_session, 106)

    assert references == {("cleanup-user", 106, "old-cleanup-model")}
    with pytest.raises(ParameterException) as exc_info:
        await assert_channel_not_referenced(db_session, channel_id=106)
    assert exc_info.value.message == ERR_MEMORY_CHANNEL_IN_USE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_status",
    [
        LongTermMemoryOldCollectionCleanupStatus.SUCCEEDED,
        LongTermMemoryOldCollectionCleanupStatus.NONE,
    ],
)
async def test_finished_or_inactive_old_collection_cleanup_does_not_protect_old_channel(
    db_session: AsyncSession,
    cleanup_status: LongTermMemoryOldCollectionCleanupStatus,
) -> None:
    await _create_store(
        db_session,
        uid="finished-cleanup-user",
        old_collection_name="old-collection",
        old_collection_cleanup_status=cleanup_status,
    )
    await memory_embedding_revision_crud.create(
        db_session,
        uid="finished-cleanup-user",
        revision=1,
        from_channel_id=107,
        from_model_id="finished-old-model",
        from_collection="old-collection",
        to_channel_id=108,
        to_model_id="new-model",
        to_collection="new-collection",
        commit=False,
    )

    assert await _references(db_session, 107) == set()
    await assert_channel_not_referenced(db_session, channel_id=107)


@pytest.mark.asyncio
async def test_same_channel_only_protects_referenced_embedding_model(
    db_session: AsyncSession,
) -> None:
    await _create_store(
        db_session,
        uid="specific-model-user",
        active_channel_id=109,
        active_model_id="embedding-used",
    )
    old_models = [
        _embedding_model("embedding-used"),
        _embedding_model("embedding-unrelated"),
        _chat_model("chat-used"),
    ]
    new_models = [
        _embedding_model("embedding-used"),
        _embedding_model("embedding-unrelated", embedding_dimensions=3072),
        _chat_model("chat-used", protocol="OPENAI_RESPONSES"),
    ]

    await assert_channel_model_identity_update_allowed(
        db_session,
        channel_id=109,
        old_model_ids=old_models,
        new_model_ids=new_models,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "delete",
        "rename",
        "usage",
        "protocol",
        "dimensions",
    ],
)
async def test_referenced_embedding_model_identity_changes_are_rejected(
    db_session: AsyncSession,
    change: str,
) -> None:
    await _create_store(
        db_session,
        uid="identity-user",
        active_channel_id=110,
        active_model_id="protected-model",
    )
    old_model = _embedding_model("protected-model")
    changed_model = dict(old_model)
    if change == "delete":
        new_models = []
    else:
        if change == "rename":
            changed_model["model_id"] = "renamed-model"
        elif change == "usage":
            changed_model["usage"] = "CHAT"
            changed_model["protocol"] = "OPENAI"
            changed_model.pop("embedding_dimensions")
        elif change == "protocol":
            changed_model["protocol"] = "CHANGED_PROTOCOL"
        else:
            changed_model["embedding_dimensions"] = 3072
        new_models = [changed_model]

    with pytest.raises(ParameterException) as exc_info:
        await assert_channel_model_identity_update_allowed(
            db_session,
            channel_id=110,
            old_model_ids=[old_model],
            new_model_ids=new_models,
        )

    assert exc_info.value.message == ERR_MEMORY_MODEL_IDENTITY_IN_USE
    assert exc_info.value.kwargs == {"model_id": "protected-model"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"is_enabled": False},
        {"description": "new description"},
        {"embedding_timeout": 90.0},
        {"advanced_settings": {"custom_headers": {"x-test": "two"}}},
    ],
)
async def test_non_identity_changes_to_referenced_embedding_model_are_allowed(
    db_session: AsyncSession,
    change: dict,
) -> None:
    await _create_store(
        db_session,
        uid="mutable-model-user",
        active_channel_id=111,
        active_model_id="protected-model",
    )
    old_model = _embedding_model("protected-model")
    new_model = {**old_model, **change}

    await assert_channel_model_identity_update_allowed(
        db_session,
        channel_id=111,
        old_model_ids=[old_model],
        new_model_ids=[new_model],
    )


@pytest.mark.asyncio
async def test_delete_channel_raises_parameter_exception_before_cleanup_or_delete(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = await _create_channel(db_session, name="protected-channel")
    channel_id = channel.id
    await _create_store(
        db_session,
        uid="delete-user",
        active_channel_id=channel_id,
        active_model_id="embedding-used",
    )
    cleanup_calls: list[str] = []

    async def unexpected_cleanup(*_args, **_kwargs):
        cleanup_calls.append("called")
        return 0

    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", unexpected_cleanup)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", unexpected_cleanup)

    with pytest.raises(ParameterException) as exc_info:
        await channels.delete_channel(channel_id, db=db_session, admin={})

    assert exc_info.value.message == ERR_MEMORY_CHANNEL_IN_USE
    assert cleanup_calls == []
    assert await channel_crud.get(db_session, channel_id) is not None


@pytest.mark.asyncio
async def test_delete_channel_without_memory_reference_deletes_after_mocked_cleanup(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = await _create_channel(db_session, name="unprotected-channel")
    cleanup_calls: list[tuple[str, int, int]] = []

    async def remove_rules(_db, channel_id, _model_ids):
        cleanup_calls.append(("profile", channel_id, len(_model_ids)))
        return 3

    async def clear_audit(_db, channel_id, _model_ids):
        cleanup_calls.append(("audit", channel_id, len(_model_ids)))
        return 2

    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", remove_rules)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", clear_audit)

    response = await channels.delete_channel(channel.id, db=db_session, admin={})

    assert response.code == 200
    assert cleanup_calls == [("profile", channel.id, 0), ("audit", channel.id, 0)]
    assert await channel_crud.get(db_session, channel.id) is None


@pytest.mark.asyncio
async def test_update_channel_protects_before_object_change_and_profile_audit_cleanup(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_embedding_model("protected-model"), _chat_model()]
    channel = await _create_channel(db_session, name="update-protected-channel", model_ids=old_models)
    channel_id = channel.id
    await _create_store(
        db_session,
        uid="update-user",
        active_channel_id=channel_id,
        active_model_id="protected-model",
    )
    cleanup_calls: list[str] = []

    async def unexpected_sync(*_args, **_kwargs):
        cleanup_calls.append("called")
        return 0

    monkeypatch.setattr(channels, "_sync_channel_model_id_renames", unexpected_sync)
    monkeypatch.setattr(channels, "_sync_audit_model_id_renames", unexpected_sync)
    monkeypatch.setattr(channels, "_remove_unavailable_channel_rules", unexpected_sync)
    monkeypatch.setattr(channels, "_clear_unavailable_audit_model_refs", unexpected_sync)

    with pytest.raises(ParameterException) as exc_info:
        await channels.update_channel(
            channel_id,
            channels.ChannelUpdate(model_ids=[_chat_model()]),
            db=db_session,
            admin={},
        )

    assert exc_info.value.message == ERR_MEMORY_MODEL_IDENTITY_IN_USE
    assert cleanup_calls == []
    unchanged = await channel_crud.get(db_session, channel_id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models


@pytest.mark.asyncio
async def test_references_from_any_uid_protect_only_the_requested_channel(
    db_session: AsyncSession,
) -> None:
    await _create_store(
        db_session,
        uid="first-user",
        active_channel_id=112,
        active_model_id="first-model",
    )
    await _create_embedding_migration_job(
        db_session,
        uid="second-user",
        dedupe_key="second-migration",
        status=LongTermMemoryMutationStatus.PENDING,
        payload={"target": {"channel_id": 112, "model_id": "second-model"}},
    )
    await _create_store(
        db_session,
        uid="unrelated-user",
        active_channel_id=113,
        active_model_id="unrelated-model",
    )

    references = await _references(db_session, 112)

    assert references == {
        ("first-user", 112, "first-model"),
        ("second-user", 112, "second-model"),
    }
    with pytest.raises(ParameterException) as exc_info:
        await assert_channel_not_referenced(db_session, channel_id=112)
    assert exc_info.value.message == ERR_MEMORY_CHANNEL_IN_USE
    await assert_channel_not_referenced(db_session, channel_id=999)


@pytest.mark.asyncio
async def test_list_memory_channel_references_uses_three_queries_for_any_uid_count(
    db_session: AsyncSession,
) -> None:
    await _create_store(
        db_session,
        uid="single-user",
        active_channel_id=114,
        active_model_id="single-model",
    )

    def count_selects(counter: list[int]):
        def before_cursor_execute(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                counter[0] += 1

        return before_cursor_execute

    sync_engine = db_session.info["sync_engine"]
    first_query_count = [0]
    first_listener = count_selects(first_query_count)
    event.listen(sync_engine, "before_cursor_execute", first_listener)
    try:
        first_references = await list_memory_channel_references(db_session, channel_id=114)
    finally:
        event.remove(sync_engine, "before_cursor_execute", first_listener)

    assert {(reference.uid, reference.channel_id, reference.model_id) for reference in first_references} == {("single-user", 114, "single-model")}
    assert first_query_count[0] == 3

    for index in range(10):
        await _create_store(
            db_session,
            uid=f"additional-user-{index}",
            active_channel_id=115 + index,
            active_model_id=f"additional-model-{index}",
        )

    second_query_count = [0]
    second_listener = count_selects(second_query_count)
    event.listen(sync_engine, "before_cursor_execute", second_listener)
    try:
        second_references = await list_memory_channel_references(db_session, channel_id=None)
    finally:
        event.remove(sync_engine, "before_cursor_execute", second_listener)

    assert len(second_references) == 11
    assert second_query_count[0] == 3


@pytest.mark.asyncio
async def test_organization_store_reference_uses_chat_usage_and_blocks_channel_delete(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(
        db_session,
        name="organization-store-channel",
        model_ids=[_chat_model("organization-chat")],
    )
    channel_id = channel.id
    await _create_store(
        db_session,
        uid="organization-store-user",
        organization_channel_id=channel_id,
        organization_model_id="organization-chat",
    )

    references = await list_memory_channel_references(db_session, channel_id=channel_id)
    assert {(reference.channel_id, reference.model_id, reference.usage) for reference in references} == {(channel_id, "organization-chat", "CHAT")}

    with pytest.raises(ParameterException) as exc_info:
        await channels.delete_channel(channel_id, db=db_session, admin={})

    assert exc_info.value.message == ERR_MEMORY_CHANNEL_IN_USE
    assert await channel_crud.get(db_session, channel_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["delete", "model_id", "usage", "protocol"])
async def test_referenced_chat_model_identity_changes_are_rejected(
    db_session: AsyncSession,
    change: str,
) -> None:
    await _create_store(
        db_session,
        uid="organization-identity-user",
        organization_channel_id=116,
        organization_model_id="protected-chat",
    )
    old_model = _chat_model("protected-chat")
    changed_model = dict(old_model)
    if change == "delete":
        new_models = []
    else:
        if change == "model_id":
            changed_model["model_id"] = "renamed-chat"
        elif change == "usage":
            changed_model["usage"] = "EMBEDDING"
            changed_model["protocol"] = "OPENAI_EMBEDDING"
            changed_model["embedding_dimensions"] = 1536
        else:
            changed_model["protocol"] = "CHANGED_PROTOCOL"
        new_models = [changed_model]

    with pytest.raises(ParameterException) as exc_info:
        await assert_channel_model_identity_update_allowed(
            db_session,
            channel_id=116,
            old_model_ids=[old_model],
            new_model_ids=new_models,
        )

    assert exc_info.value.message == ERR_MEMORY_MODEL_IDENTITY_IN_USE
    assert exc_info.value.kwargs == {"model_id": "protected-chat"}


@pytest.mark.asyncio
async def test_update_channel_previews_and_confirms_exact_chat_model_rename(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_organization_chat_model("protected-chat")]
    new_models = [_organization_chat_model("renamed-chat")]
    channel = await _create_channel(
        db_session,
        name="organization-update-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="organization-update-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="protected-chat",
    )
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, synced=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "protected-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models, confirm_config_impact=True),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, synced=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "renamed-chat"
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_update_channel_does_not_sync_ambiguous_many_to_one_chat_model_replacement_to_single_new_id(
    db_session: AsyncSession,
) -> None:
    old_models = [_organization_chat_model("old-a"), _organization_chat_model("old-b")]
    new_models = [_organization_chat_model("new-c")]
    channel = await _create_channel(
        db_session,
        name="organization-ambiguous-update-channel",
        model_ids=old_models,
    )
    store_a = await _create_store(
        db_session,
        uid="organization-ambiguous-update-user-a",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="old-a",
    )
    store_b = await _create_store(
        db_session,
        uid="organization-ambiguous-update-user-b",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="old-b",
    )
    profile = Profile(
        uid="organization-ambiguous-profile-user",
        name="organization-ambiguous-profile",
        configs={
            "channel": {
                "chat_channel": {
                    "rules": [{"channel_id": channel.id, "model_id": "old-a"}],
                },
            },
            "security": {
                "audit_channel_id": channel.id,
                "audit_model_id": "old-b",
            },
        },
    )
    db_session.add(profile)
    await db_session.flush()

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    assert preview.code == 200
    assert preview.data["requires_confirmation"] is True
    assert preview.data["synced_memory_organization_settings"] == 0
    assert preview.data["retained_memory_organization_settings"] == 0
    assert preview.data["disabled_memory_organization_settings"] == 2
    assert preview.data["synced_profile_rules"] == 0
    assert preview.data["removed_profile_rules"] == 1
    assert preview.data["synced_audit_refs"] == 0
    assert preview.data["cleared_audit_refs"] == 1
    await db_session.refresh(store_a)
    await db_session.refresh(store_b)
    await db_session.refresh(profile)
    assert store_a.auto_organize_enabled is True
    assert store_a.organization_channel_id == channel.id
    assert store_a.organization_model_id == "old-a"
    assert store_b.auto_organize_enabled is True
    assert store_b.organization_channel_id == channel.id
    assert store_b.organization_model_id == "old-b"
    assert profile.configs["channel"]["chat_channel"]["rules"] == [{"channel_id": channel.id, "model_id": "old-a"}]
    assert profile.configs["security"]["audit_channel_id"] == channel.id
    assert profile.configs["security"]["audit_model_id"] == "old-b"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models, confirm_config_impact=True),
        db=db_session,
        admin={},
    )

    assert response.code == 200
    assert response.data["requires_confirmation"] is False
    assert response.data["synced_memory_organization_settings"] == 0
    assert response.data["retained_memory_organization_settings"] == 0
    assert response.data["disabled_memory_organization_settings"] == 2
    assert response.data["synced_profile_rules"] == 0
    assert response.data["removed_profile_rules"] == 1
    assert response.data["synced_audit_refs"] == 0
    assert response.data["cleared_audit_refs"] == 1
    await db_session.refresh(store_a)
    await db_session.refresh(store_b)
    await db_session.refresh(profile)
    assert store_a.auto_organize_enabled is False
    assert store_a.organization_channel_id is None
    assert store_a.organization_model_id is None
    assert store_b.auto_organize_enabled is False
    assert store_b.organization_channel_id is None
    assert store_b.organization_model_id is None
    assert profile.configs["channel"]["chat_channel"]["rules"] == []
    assert profile.configs["security"]["audit_channel_id"] is None
    assert profile.configs["security"]["audit_model_id"] is None
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"description": "updated description"}, id="description"),
        pytest.param({"image_understanding": True}, id="image-understanding"),
        pytest.param({"audio_understanding": True}, id="audio-understanding"),
        pytest.param({"video_understanding": True}, id="video-understanding"),
    ],
)
async def test_update_channel_allows_non_identity_chat_model_metadata_without_memory_confirmation(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    change: dict,
) -> None:
    old_models = [_organization_chat_model("mutable-chat")]
    new_models = [{**old_models[0], **change}]
    channel = await _create_channel(
        db_session,
        name="organization-metadata-update-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="organization-metadata-update-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="mutable-chat",
    )
    _patch_profile_sync_noops(monkeypatch)

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "mutable-chat"
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"protocol": "OPENAI_RESPONSES"}, id="protocol"),
        pytest.param({"temperature": 1.2, "top_p": 0.4}, id="runtime-parameters"),
    ],
)
async def test_update_channel_previews_and_confirms_retained_chat_model_changes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    change: dict,
) -> None:
    old_models = [_organization_chat_model("mutable-chat")]
    new_models = [{**old_models[0], **change}]
    channel = await _create_channel(
        db_session,
        name="organization-retained-update-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="organization-retained-update-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="mutable-chat",
    )
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, retained=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "mutable-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models, confirm_config_impact=True),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, retained=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "mutable-chat"
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        pytest.param("delete", id="delete"),
        pytest.param("usage", id="usage-change"),
        pytest.param("rename_with_parameters", id="rename-with-parameters"),
    ],
)
async def test_update_channel_previews_and_confirms_disabling_invalid_chat_model_changes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    old_models = [_organization_chat_model("protected-chat")]
    channel = await _create_channel(
        db_session,
        name=f"organization-disabled-update-channel-{change}",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid=f"organization-disabled-update-user-{change}",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="protected-chat",
    )
    _patch_profile_sync_noops(monkeypatch)

    if change == "delete":
        new_models = []
    elif change == "usage":
        new_models = [_embedding_model("protected-chat")]
    else:
        new_models = [_organization_chat_model("renamed-chat", temperature=1.2)]

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "protected-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models, confirm_config_impact=True),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is False
    assert store.organization_channel_id is None
    assert store.organization_model_id is None
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_update_channel_defers_chat_model_delete_until_organization_job_finishes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = "organization-deferred-delete-chat"
    active_uid = "organization-deferred-delete-active-user"
    other_uid = "organization-deferred-delete-other-user"
    old_models = [_organization_chat_model(model_id)]
    channel = await _create_channel(
        db_session,
        name="organization-deferred-delete-channel",
        model_ids=old_models,
    )
    active_store = await _create_store(
        db_session,
        uid=active_uid,
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id=model_id,
    )
    other_store = await _create_store(
        db_session,
        uid=other_uid,
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id=model_id,
    )
    job = await _create_organization_job(
        db_session,
        uid=active_uid,
        dedupe_key="organization-deferred-delete",
        status=LongTermMemoryMutationStatus.PENDING,
        channel_id=channel.id,
        model_id=model_id,
    )
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=[]),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(
        preview,
        requires_confirmation=True,
        disabled=1,
        deferred=1,
        pending_deletion_models=1,
    )
    await db_session.refresh(active_store)
    await db_session.refresh(other_store)
    await db_session.refresh(job)
    assert active_store.auto_organize_enabled is True
    assert active_store.organization_channel_id == channel.id
    assert active_store.organization_model_id == model_id
    assert other_store.auto_organize_enabled is True
    assert other_store.organization_channel_id == channel.id
    assert other_store.organization_model_id == model_id
    assert job.status == LongTermMemoryMutationStatus.PENDING
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=[], confirm_config_impact=True),
        db=db_session,
        admin={},
    )

    _assert_update_impact(
        response,
        requires_confirmation=False,
        disabled=1,
        deferred=1,
        pending_deletion_models=1,
    )
    await db_session.refresh(active_store)
    await db_session.refresh(other_store)
    assert active_store.auto_organize_enabled is False
    assert active_store.organization_channel_id == channel.id
    assert active_store.organization_model_id == model_id
    assert other_store.auto_organize_enabled is False
    assert other_store.organization_channel_id is None
    assert other_store.organization_model_id is None
    pending = await channel_crud.get(db_session, channel.id)
    assert pending is not None
    assert pending.model_ids[0]["model_id"] == model_id
    assert pending.model_ids[0]["lifecycle_status"] == "pending_delete"
    assert pending.model_ids[0]["is_enabled"] is False

    completed_job = await memory_job_crud.update_status(
        db_session,
        uid=active_uid,
        job_id=job.id,
        status=LongTermMemoryMutationStatus.SUCCEEDED,
        commit=False,
        clear_active_mutation_key=True,
    )
    assert completed_job is not None
    deleted_count = await finalize_pending_channel_model_deletions_for_organization_job(
        db_session,
        job=completed_job,
    )
    await db_session.commit()

    assert deleted_count == 1
    finalized = await channel_crud.get(db_session, channel.id)
    assert finalized is not None
    assert finalized.model_ids == []
    await db_session.refresh(active_store)
    assert active_store.auto_organize_enabled is False
    assert active_store.organization_channel_id is None
    assert active_store.organization_model_id is None


@pytest.mark.asyncio
async def test_pending_delete_chat_model_rejects_submitted_modification(
    db_session: AsyncSession,
) -> None:
    pending_model = _organization_chat_model(
        "locked-pending-chat",
        lifecycle_status="pending_delete",
        is_enabled=False,
    )
    channel = await _create_channel(
        db_session,
        name="locked-pending-chat-channel",
        model_ids=[pending_model],
    )

    with pytest.raises(ParameterException) as exc_info:
        await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(
                model_ids=[{**pending_model, "description": "changed description"}],
            ),
            db=db_session,
            admin={},
        )

    assert exc_info.value.message == ERR_CHANNEL_MODEL_LOCKED


@pytest.mark.asyncio
async def test_update_channel_base_url_validation_rolls_back_deferred_chat_model_delete(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_id = "atomic-base-url-chat"
    old_models = [_organization_chat_model(model_id)]
    channel = await _create_channel(
        db_session,
        name="atomic-base-url-channel",
        model_ids=old_models,
    )
    original_base_url = channel.base_url
    store = await _create_store(
        db_session,
        uid="atomic-base-url-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id=model_id,
    )
    job = await _create_organization_job(
        db_session,
        uid=store.uid,
        dedupe_key="atomic-base-url-organization",
        status=LongTermMemoryMutationStatus.PENDING,
        channel_id=channel.id,
        model_id=model_id,
    )
    _patch_profile_sync_noops(monkeypatch)
    await db_session.commit()

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            base_url=None,
            model_ids=[],
            confirm_config_impact=True,
        ),
        db=db_session,
        admin={},
    )

    assert response.code == 422
    assert response.message == t(ERR_CHANNEL_BASE_URL_REQUIRED_FOR_MODELS)
    await db_session.refresh(channel)
    await db_session.refresh(store)
    await db_session.refresh(job)
    assert channel.base_url == original_base_url
    assert channel.model_ids == old_models
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == model_id
    assert job.status == LongTermMemoryMutationStatus.PENDING


@pytest.mark.asyncio
async def test_confirmed_chat_model_delete_does_not_overwrite_concurrent_store_selection(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_model_id = "organization-delete-chat"
    fallback_model_id = "organization-fallback-chat"
    original_channel = await _create_channel(
        db_session,
        name="organization-delete-race-channel",
        model_ids=[_organization_chat_model(original_model_id)],
    )
    fallback_channel = await _create_channel(
        db_session,
        name="organization-fallback-race-channel",
        model_ids=[_organization_chat_model(fallback_model_id)],
    )
    store = await _create_store(
        db_session,
        uid="organization-delete-race-user",
        auto_organize_enabled=True,
        organization_channel_id=original_channel.id,
        organization_model_id=original_model_id,
    )
    _patch_profile_sync_noops(monkeypatch)

    original_conditional_update = memory_store_crud.update_auto_organize_if_channel_and_model
    conditional_update_calls = 0

    async def switch_store_before_first_conditional_update(*args, **kwargs) -> bool:
        nonlocal conditional_update_calls
        conditional_update_calls += 1
        if conditional_update_calls == 1:
            await memory_store_crud.update_by_uid(
                db_session,
                uid=store.uid,
                auto_organize_enabled=True,
                organization_channel_id=fallback_channel.id,
                organization_model_id=fallback_model_id,
                commit=False,
            )
        return await original_conditional_update(*args, **kwargs)

    monkeypatch.setattr(
        memory_store_crud,
        "update_auto_organize_if_channel_and_model",
        switch_store_before_first_conditional_update,
    )

    response = await channels.update_channel(
        original_channel.id,
        channels.ChannelUpdate(model_ids=[], confirm_config_impact=True),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, synced=0, disabled=0)
    assert conditional_update_calls == 1
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == fallback_channel.id
    assert store.organization_model_id == fallback_model_id
    updated = await channel_crud.get(db_session, original_channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids([])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "old_model_ids",
        "new_model_ids",
        "concurrent_model_id",
        "expected_enabled",
        "expected_model_id",
        "expected_disabled",
        "expected_concurrently_disabled",
    ),
    [
        pytest.param(
            ["old-a", "old-b"],
            [],
            "old-b",
            False,
            None,
            1,
            1,
            id="concurrent-selection-is-disabled",
        ),
        pytest.param(
            ["old-a", "keep-b"],
            ["keep-b"],
            "keep-b",
            True,
            "keep-b",
            0,
            0,
            id="concurrent-selection-is-retained",
        ),
    ],
)
async def test_confirmed_chat_model_update_handles_concurrent_same_channel_store_selection(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    old_model_ids: list[str],
    new_model_ids: list[str],
    concurrent_model_id: str,
    expected_enabled: bool,
    expected_model_id: str | None,
    expected_disabled: int,
    expected_concurrently_disabled: int,
) -> None:
    original_channel = await _create_channel(
        db_session,
        name="organization-same-channel-race-channel",
        model_ids=[_organization_chat_model(model_id) for model_id in old_model_ids],
    )
    store = await _create_store(
        db_session,
        uid="organization-same-channel-race-user",
        auto_organize_enabled=True,
        organization_channel_id=original_channel.id,
        organization_model_id="old-a",
    )
    _patch_profile_sync_noops(monkeypatch)

    original_conditional_update = memory_store_crud.update_auto_organize_if_channel_and_model
    conditional_update_calls = 0

    async def switch_store_before_first_conditional_update(*args, **kwargs) -> bool:
        nonlocal conditional_update_calls
        conditional_update_calls += 1
        if conditional_update_calls == 1:
            await memory_store_crud.update_by_uid(
                db_session,
                uid=store.uid,
                auto_organize_enabled=True,
                organization_channel_id=original_channel.id,
                organization_model_id=concurrent_model_id,
                commit=False,
            )
        return await original_conditional_update(*args, **kwargs)

    monkeypatch.setattr(
        memory_store_crud,
        "update_auto_organize_if_channel_and_model",
        switch_store_before_first_conditional_update,
    )

    new_models = [_organization_chat_model(model_id) for model_id in new_model_ids]
    response = await channels.update_channel(
        original_channel.id,
        channels.ChannelUpdate(model_ids=new_models, confirm_config_impact=True),
        db=db_session,
        admin={},
    )

    _assert_update_impact(
        response,
        requires_confirmation=False,
        disabled=expected_disabled,
        concurrently_disabled=expected_concurrently_disabled,
    )
    assert conditional_update_calls == 1
    await db_session.refresh(store)
    assert store.auto_organize_enabled is expected_enabled
    assert store.organization_channel_id == (original_channel.id if expected_enabled else None)
    assert store.organization_model_id == expected_model_id
    updated = await channel_crud.get(db_session, original_channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_update_channel_organization_budget_validation_uses_each_uid_active_count(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_organization_chat_model("budget-chat", max_tokens=4096)]
    new_models = [_organization_chat_model("budget-chat", max_tokens=255)]
    channel = await _create_channel(
        db_session,
        name="organization-budget-update-channel",
        model_ids=old_models,
    )
    active_store = await _create_store(
        db_session,
        uid="organization-budget-active-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="budget-chat",
    )
    empty_store = await _create_store(
        db_session,
        uid="organization-budget-empty-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="budget-chat",
    )
    await _create_active_memory_record(
        db_session,
        uid="organization-budget-active-user",
        memory_key="budget-memory",
    )
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, retained=1, disabled=1)
    await db_session.refresh(active_store)
    await db_session.refresh(empty_store)
    assert active_store.auto_organize_enabled is True
    assert active_store.organization_channel_id == channel.id
    assert active_store.organization_model_id == "budget-chat"
    assert empty_store.auto_organize_enabled is True
    assert empty_store.organization_channel_id == channel.id
    assert empty_store.organization_model_id == "budget-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models, confirm_config_impact=True),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, retained=1, disabled=1)
    await db_session.refresh(active_store)
    await db_session.refresh(empty_store)
    assert active_store.auto_organize_enabled is False
    assert active_store.organization_channel_id is None
    assert active_store.organization_model_id is None
    assert empty_store.auto_organize_enabled is True
    assert empty_store.organization_channel_id == channel.id
    assert empty_store.organization_model_id == "budget-chat"
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_update_channel_organization_budget_validation_rejects_real_input_overflow_at_boundary(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_organization_chat_model("budget-boundary-chat")]
    new_models = [_organization_chat_model("budget-boundary-chat", context_window_k=13, max_tokens=11520)]
    channel = await _create_channel(
        db_session,
        name="organization-budget-boundary-update-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="organization-budget-boundary-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="budget-boundary-chat",
    )
    records = []
    for index in range(45):
        records.append(
            await _create_active_memory_record(
                db_session,
                uid="organization-budget-boundary-user",
                memory_key=f"budget-boundary-memory-{index}",
            )
        )
    snapshot_items = build_organization_snapshot_items(records)
    required_input_tokens = calculate_organization_required_input_tokens(snapshot_items)
    assert required_input_tokens > 1224
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "budget-boundary-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models, confirm_config_impact=True),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is False
    assert store.organization_channel_id is None
    assert store.organization_model_id is None
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_update_channel_requires_context_window_for_output_safety_margin_and_input_space(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_models = [_organization_chat_model("context-budget-chat")]
    new_models = [
        _organization_chat_model(
            "context-budget-chat",
            context_window_k=1,
            max_tokens=743,
        )
    ]
    channel = await _create_channel(
        db_session,
        name="organization-context-budget-update-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="organization-context-budget-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="context-budget-chat",
    )
    for index in range(3):
        await _create_active_memory_record(
            db_session,
            uid="organization-context-budget-user",
            memory_key=f"context-budget-memory-{index}",
        )
    _patch_profile_sync_noops(monkeypatch)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=new_models),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(preview, requires_confirmation=True, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "context-budget-chat"
    unchanged = await channel_crud.get(db_session, channel.id)
    assert unchanged is not None
    assert unchanged.model_ids == old_models

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=new_models, confirm_config_impact=True),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False, disabled=1)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is False
    assert store.organization_channel_id is None
    assert store.organization_model_id is None
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == normalize_channel_model_ids(new_models)


@pytest.mark.asyncio
async def test_active_organization_job_rejects_chat_model_update_and_rolls_back_settings(
    db_session: AsyncSession,
) -> None:
    old_models = [_organization_chat_model("job-chat")]
    channel = await _create_channel(
        db_session,
        name="active-organization-job-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="active-organization-job-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="job-chat",
    )
    job = await _create_organization_job(
        db_session,
        uid="active-organization-job-user",
        dedupe_key="active-organization-job",
        status=LongTermMemoryMutationStatus.PENDING,
        channel_id=channel.id,
        model_id="job-chat",
    )
    await db_session.commit()
    references = await list_memory_channel_references(db_session, channel_id=channel.id)
    reference_values = {(reference.uid, reference.channel_id, reference.model_id, reference.usage, reference.is_adaptable) for reference in references}
    assert reference_values == {
        ("active-organization-job-user", channel.id, "job-chat", "CHAT", True),
        ("active-organization-job-user", channel.id, "job-chat", "CHAT", False),
    }
    assert {reference.is_adaptable for reference in references} == {True, False}
    original_job_status = job.status
    original_job_payload = dict(job.payload)

    with _capture_writes(db_session) as writes:
        preview = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(model_ids=[]),
            db=db_session,
            admin={},
        )

    _assert_only_channel_lock_writes(writes)
    _assert_update_impact(
        preview,
        requires_confirmation=True,
        deferred=1,
        pending_deletion_models=1,
    )

    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "job-chat"
    await db_session.refresh(job)
    assert job.status == original_job_status
    assert job.payload == original_job_payload
    references_after = await list_memory_channel_references(db_session, channel_id=channel.id)
    assert {(reference.uid, reference.channel_id, reference.model_id, reference.usage, reference.is_adaptable) for reference in references_after} == reference_values
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == old_models


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"is_enabled": False},
        {"description": "updated description"},
        {"temperature": 1.2},
        {"top_p": 0.4},
        {"context_window_k": 64},
        {"max_tokens": 4096},
        {"advanced_settings": {"custom_headers": {"x-test": "two"}}},
    ],
)
async def test_non_identity_changes_to_referenced_chat_model_are_allowed(
    db_session: AsyncSession,
    change: dict,
) -> None:
    await _create_store(
        db_session,
        uid="organization-mutable-user",
        organization_channel_id=117,
        organization_model_id="mutable-chat",
    )
    old_model = _chat_model("mutable-chat")
    new_model = {**old_model, **change}

    await assert_channel_model_identity_update_allowed(
        db_session,
        channel_id=117,
        old_model_ids=[old_model],
        new_model_ids=[new_model],
    )


@pytest.mark.asyncio
async def test_unrelated_embedding_model_identity_change_on_chat_referenced_channel_is_allowed(
    db_session: AsyncSession,
) -> None:
    await _create_store(
        db_session,
        uid="organization-unrelated-user",
        organization_channel_id=118,
        organization_model_id="used-chat",
    )
    old_models = [
        _chat_model("used-chat"),
        _embedding_model("unrelated-embedding"),
    ]
    new_models = [
        _chat_model("used-chat"),
        _embedding_model(
            "renamed-embedding",
            protocol="OPENAI_EMBEDDING",
            embedding_dimensions=3072,
        ),
    ]

    await assert_channel_model_identity_update_allowed(
        db_session,
        channel_id=118,
        old_model_ids=old_models,
        new_model_ids=new_models,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        LongTermMemoryMutationStatus.PENDING,
        LongTermMemoryMutationStatus.RUNNING,
        LongTermMemoryMutationStatus.RETRY,
        LongTermMemoryMutationStatus.SUCCEEDED,
        LongTermMemoryMutationStatus.FAILED,
        LongTermMemoryMutationStatus.CANCELLED,
    ],
)
async def test_organization_job_model_snapshot_protection_depends_on_active_status(
    db_session: AsyncSession,
    status: LongTermMemoryMutationStatus,
) -> None:
    await _create_organization_job(
        db_session,
        uid="organization-job-user",
        dedupe_key=f"organization-{status.value}",
        status=status,
        channel_id=119,
        model_id="job-chat",
    )

    references = await _references(db_session, 119)
    if status in {
        LongTermMemoryMutationStatus.PENDING,
        LongTermMemoryMutationStatus.RUNNING,
        LongTermMemoryMutationStatus.RETRY,
    }:
        detailed_references = await list_memory_channel_references(db_session, channel_id=119)
        assert {(reference.channel_id, reference.model_id, reference.usage) for reference in detailed_references} == {(119, "job-chat", "CHAT")}
        assert references == {("organization-job-user", 119, "job-chat")}
        with pytest.raises(ParameterException) as exc_info:
            await assert_channel_not_referenced(db_session, channel_id=119)
        assert exc_info.value.message == ERR_MEMORY_CHANNEL_IN_USE
    else:
        assert references == set()
        await assert_channel_not_referenced(db_session, channel_id=119)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        LongTermMemoryMutationStatus.PENDING,
        LongTermMemoryMutationStatus.RUNNING,
        LongTermMemoryMutationStatus.RETRY,
    ],
)
@pytest.mark.parametrize("change", ["delete", "rename", "usage", "protocol"])
async def test_active_organization_job_snapshot_protects_chat_model_identity_changes(
    db_session: AsyncSession,
    status: LongTermMemoryMutationStatus,
    change: str,
) -> None:
    await _create_organization_job(
        db_session,
        uid=f"organization-job-identity-{status.value}-{change}",
        dedupe_key=f"organization-identity-{status.value}-{change}",
        status=status,
        channel_id=121,
        model_id="job-chat",
    )
    old_model = _chat_model("job-chat")
    changed_model = dict(old_model)
    if change == "delete":
        new_models = []
    else:
        if change == "rename":
            changed_model["model_id"] = "renamed-job-chat"
        elif change == "usage":
            changed_model["usage"] = "EMBEDDING"
            changed_model["protocol"] = "OPENAI_EMBEDDING"
            changed_model["embedding_dimensions"] = 1536
        else:
            changed_model["protocol"] = "CHANGED_PROTOCOL"
        new_models = [changed_model]

    with pytest.raises(ParameterException) as exc_info:
        await assert_channel_model_identity_update_allowed(
            db_session,
            channel_id=121,
            old_model_ids=[old_model],
            new_model_ids=new_models,
        )

    assert exc_info.value.message == ERR_MEMORY_MODEL_IDENTITY_IN_USE
    assert exc_info.value.kwargs == {"model_id": "job-chat"}


@pytest.mark.asyncio
async def test_organization_job_snapshot_keeps_old_reference_after_store_selection_changes(
    db_session: AsyncSession,
) -> None:
    await _create_store(
        db_session,
        uid="organization-snapshot-user",
        organization_channel_id=120,
        organization_model_id="old-chat",
    )
    await _create_organization_job(
        db_session,
        uid="organization-snapshot-user",
        dedupe_key="organization-old-snapshot",
        status=LongTermMemoryMutationStatus.PENDING,
        channel_id=120,
        model_id="old-chat",
    )

    await memory_store_crud.update_by_uid(
        db_session,
        uid="organization-snapshot-user",
        auto_organize_enabled=False,
        organization_channel_id=None,
        organization_model_id=None,
        commit=False,
    )
    assert await _references(db_session, 120) == {("organization-snapshot-user", 120, "old-chat")}

    await memory_store_crud.update_by_uid(
        db_session,
        uid="organization-snapshot-user",
        auto_organize_enabled=True,
        organization_channel_id=121,
        organization_model_id="new-chat",
        commit=False,
    )
    assert await _references(db_session, 120) == {("organization-snapshot-user", 120, "old-chat")}
    assert await _references(db_session, 121) == {("organization-snapshot-user", 121, "new-chat")}

    for channel_id in (120, 121):
        with pytest.raises(ParameterException) as exc_info:
            await assert_channel_not_referenced(db_session, channel_id=channel_id)
        assert exc_info.value.message == ERR_MEMORY_CHANNEL_IN_USE


@pytest.mark.asyncio
async def test_organization_referenced_channel_allows_connection_field_maintenance(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(
        db_session,
        name="organization-connection-channel",
        model_ids=[_chat_model("connection-chat")],
    )
    await _create_store(
        db_session,
        uid="organization-connection-user",
        organization_channel_id=channel.id,
        organization_model_id="connection-chat",
    )

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(
            base_url="https://changed.example/v1",
            api_key="changed-api-key",
            http_proxy="http://changed-proxy.example:8081",
        ),
        db=db_session,
        admin={},
    )

    assert response.code == 200
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.base_url == "https://changed.example/v1"
    assert updated.get_decrypted_api_key() == "changed-api-key"
    assert updated.http_proxy == "http://changed-proxy.example:8081"


@pytest.mark.asyncio
async def test_connection_field_update_locks_channel_before_mutation_without_model_ids(
    db_session: AsyncSession,
) -> None:
    channel = await _create_channel(
        db_session,
        name="connection-lock-channel",
        model_ids=[_chat_model("connection-lock-chat")],
    )

    with _capture_writes(db_session) as writes:
        response = await channels.update_channel(
            channel.id,
            channels.ChannelUpdate(http_proxy="http://locked-proxy.example:8081"),
            db=db_session,
            admin={},
        )

    assert response.code == 200
    normalized_writes = [" ".join(statement.strip().upper().split()) for statement in writes]
    assert normalized_writes
    assert normalized_writes[0].startswith("UPDATE CHANNEL SET ID=CHANNEL.ID")
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.http_proxy == "http://locked-proxy.example:8081"


@pytest.mark.asyncio
async def test_update_channel_with_unchanged_models_does_not_decrypt_api_key(
    db_session: AsyncSession,
) -> None:
    old_models = [_organization_chat_model("unchanged-organization-chat")]
    channel = await _create_channel(
        db_session,
        name="organization-unchanged-models-channel",
        model_ids=old_models,
    )
    store = await _create_store(
        db_session,
        uid="organization-unchanged-models-user",
        auto_organize_enabled=True,
        organization_channel_id=channel.id,
        organization_model_id="unchanged-organization-chat",
    )
    channel.api_key = "not-a-valid-encrypted-api-key"
    await db_session.flush()

    response = await channels.update_channel(
        channel.id,
        channels.ChannelUpdate(model_ids=[dict(old_models[0])]),
        db=db_session,
        admin={},
    )

    _assert_update_impact(response, requires_confirmation=False)
    await db_session.refresh(store)
    assert store.auto_organize_enabled is True
    assert store.organization_channel_id == channel.id
    assert store.organization_model_id == "unchanged-organization-chat"
    updated = await channel_crud.get(db_session, channel.id)
    assert updated is not None
    assert updated.model_ids == old_models
