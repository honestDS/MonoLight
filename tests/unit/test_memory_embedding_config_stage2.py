import hashlib
from collections.abc import AsyncGenerator
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlmodel import SQLModel, select

from app.core.constants import (
    ERR_PROFILE_MEMORY_ACTIVE_NOT_CONFIGURED,
    ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED,
    ERR_PROFILE_MEMORY_MIGRATION_ACTIVE,
    ERR_PROFILE_MEMORY_MIGRATION_CONFLICT,
    ERR_PROFILE_MEMORY_SELECTION_EXPIRED,
    ERR_PROFILE_MEMORY_SELECTION_INVALID,
    ERR_PROFILE_MEMORY_SELECTION_STALE,
)
from app.core.crud.memory import (
    memory_embedding_revision_crud,
    memory_embedding_selection_token_crud,
    memory_store_crud,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.crud.profile import profile_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.exceptions import ParameterException, ResourceNotFoundException
from app.core.memory import embedding_config as embedding_service
from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryEmbeddingRevisionStatus,
    LongTermMemoryEmbeddingSelectionToken,
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryStore,
)
from app.models.profile import (
    LongTermMemoryConfig,
    LongTermMemoryOrganizationConfig,
    Profile,
    ProfileConfig,
    ProfileCreate,
    ProfileResponse,
    ProfileUpdate,
)
from app.schemas.memory import MemorySettingsUpdateRequest
from scripts import migration_20260803_add_memory_embedding_selection_token as selection_token_migration

MEMORY_CONFIG_TABLES = [
    Profile.__table__,
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


async def _create_profile(
    db: AsyncSession,
    *,
    uid: str,
    name: str,
    memory: LongTermMemoryConfig | None = None,
) -> Profile:
    return await profile_crud.create(
        db,
        obj_in={"uid": uid, "name": name, "configs": _profile_configs(memory)},
    )


async def _get_store(db: AsyncSession, uid: str) -> LongTermMemoryStore | None:
    return await memory_store_crud.get_snapshot_by_uid(db, uid=uid)


async def _get_selection(db: AsyncSession, *, uid: str, profile_id: int, token: str) -> LongTermMemoryEmbeddingSelectionToken | None:
    return await memory_embedding_selection_token_crud.get_by_digest(
        db,
        uid=uid,
        profile_id=profile_id,
        token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )


def _patch_embedding_probe(monkeypatch, *, dimensions: int = 3) -> list[tuple[str, object]]:
    calls: list[tuple[str, object]] = []

    async def fake_load(_db, channel_id: int, model_id: str) -> EmbeddingRuntimeConfig:
        calls.append(("load", (channel_id, model_id)))
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

    async def fake_detect(config: EmbeddingRuntimeConfig) -> int:
        calls.append(("detect", config))
        return dimensions

    monkeypatch.setattr(embedding_service, "load_embedding_runtime_config", fake_load)
    monkeypatch.setattr(embedding_service, "detect_embedding_dimensions", fake_detect)
    return calls


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=MEMORY_CONFIG_TABLES))

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def test_profile_config_memory_defaults_and_legacy_flat_fields() -> None:
    old_config = ProfileConfig.model_validate({})

    assert old_config.memory.enabled is False
    assert old_config.memory.embedding_channel_id is None
    assert old_config.memory.embedding_model_id is None
    assert old_config.memory.top_k == 5
    assert old_config.memory.candidate_k == 10
    assert old_config.memory.result_max_chars == 4000

    flat_config = ProfileConfig.model_validate(
        {
            "memory_enabled": True,
            "memory_embedding_channel_id": 17,
            "memory_embedding_model_id": "embed-v2",
            "memory_top_k": 7,
            "memory_candidate_k": 12,
            "memory_result_max_chars": 8192,
        }
    )

    assert flat_config.memory.model_dump() == {
        "enabled": True,
        "embedding_channel_id": 17,
        "embedding_model_id": "embed-v2",
        "top_k": 7,
        "candidate_k": 12,
        "result_max_chars": 8192,
    }


def test_memory_organization_contract_uses_canonical_fields_and_defaults() -> None:
    organization = LongTermMemoryOrganizationConfig.model_validate({})

    assert set(LongTermMemoryOrganizationConfig.model_fields) == {
        "auto_organize_enabled",
        "organization_channel_id",
        "organization_model_id",
    }
    assert organization.model_dump() == {
        "auto_organize_enabled": False,
        "organization_channel_id": None,
        "organization_model_id": None,
    }
    assert set(MemorySettingsUpdateRequest.model_fields) == set(LongTermMemoryOrganizationConfig.model_fields)
    assert MemorySettingsUpdateRequest.model_validate({}).model_dump() == organization.model_dump()
    assert "channel_id" not in LongTermMemoryOrganizationConfig.model_fields
    assert "model_id" not in LongTermMemoryOrganizationConfig.model_fields


@pytest.mark.parametrize("model", [LongTermMemoryOrganizationConfig, MemorySettingsUpdateRequest])
@pytest.mark.parametrize(
    "values",
    [
        {"organization_channel_id": 1},
        {"organization_model_id": "chat-model"},
    ],
)
def test_memory_organization_requires_channel_and_model_as_a_pair(model, values: dict) -> None:
    with pytest.raises(ValueError):
        model.model_validate(values)


def test_memory_settings_update_request_forbids_extra_fields() -> None:
    with pytest.raises(ValueError):
        MemorySettingsUpdateRequest.model_validate({"unexpected": True})


def test_profile_memory_organization_is_exposed_at_profile_boundary_only() -> None:
    assert "memory_organization" in ProfileCreate.model_fields
    assert "memory_organization" in ProfileUpdate.model_fields
    assert "memory_organization" in ProfileResponse.model_fields
    assert "auto_organize_enabled" not in LongTermMemoryConfig.model_fields
    assert "organization_channel_id" not in LongTermMemoryConfig.model_fields
    assert "organization_model_id" not in LongTermMemoryConfig.model_fields
    assert "auto_organize_enabled" not in ProfileConfig.model_validate({}).memory.model_dump()
    assert "organization_channel_id" not in ProfileConfig.model_validate({}).memory.model_dump()
    assert "organization_model_id" not in ProfileConfig.model_validate({}).memory.model_dump()
    assert set(ProfileConfig.model_fields["memory"].annotation.model_fields) == set(LongTermMemoryConfig.model_fields)


@pytest.mark.parametrize(
    "values",
    [
        {"embedding_channel_id": 1},
        {"embedding_model_id": "embed-v1"},
        {"embedding_channel_id": None, "embedding_model_id": "embed-v1"},
        {"embedding_channel_id": 1, "embedding_model_id": None},
    ],
)
def test_profile_config_requires_embedding_channel_and_model_as_a_pair(values: dict) -> None:
    with pytest.raises(ValueError):
        LongTermMemoryConfig.model_validate(values)


def test_profile_config_rejects_candidate_budget_below_top_k() -> None:
    with pytest.raises(ValueError):
        LongTermMemoryConfig.model_validate({"top_k": 8, "candidate_k": 7})


@pytest.mark.asyncio
async def test_selection_token_migration_is_idempotent_has_no_foreign_keys_and_preserves_rows() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"))
            await connection.execute(
                text("INSERT INTO legacy_marker (id, value) VALUES (:id, :value)"),
                {"id": 1, "value": "preserved"},
            )
        async with session_factory() as session:
            await selection_token_migration.migrate(session)
            await session.commit()
            await session.execute(
                text(
                    "INSERT INTO long_term_memory_embedding_selection_token "
                    "(uid, profile_id, token_digest, profile_config_digest, target_embedding_channel_id, "
                    "target_embedding_model_id, target_embedding_dimensions, target_embedding_signature, expires_at) "
                    "VALUES (:uid, :profile_id, :token_digest, :profile_config_digest, :channel_id, :model_id, :dimensions, :signature, :expires_at)"
                ),
                {
                    "uid": "user-a",
                    "profile_id": 11,
                    "token_digest": "a" * 64,
                    "profile_config_digest": "b" * 64,
                    "channel_id": 7,
                    "model_id": "embed-v1",
                    "dimensions": 3,
                    "signature": "c" * 64,
                    "expires_at": "2099-01-01 00:00:00",
                },
            )
            await session.commit()
            await selection_token_migration.migrate(session)
            await session.commit()

        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "tables": set(inspect(sync_connection).get_table_names()),
                    "columns": {column["name"] for column in inspect(sync_connection).get_columns("long_term_memory_embedding_selection_token")},
                    "foreign_keys": inspect(sync_connection).get_foreign_keys("long_term_memory_embedding_selection_token"),
                    "indexes": {index["name"] for index in inspect(sync_connection).get_indexes("long_term_memory_embedding_selection_token")},
                    "unique": {(item["name"], tuple(item["column_names"])) for item in inspect(sync_connection).get_unique_constraints("long_term_memory_embedding_selection_token") if item.get("name")},
                }
            )
            row = (await connection.execute(text("SELECT uid, profile_id, token_digest FROM long_term_memory_embedding_selection_token"))).mappings().one()
            legacy_value = (await connection.execute(text("SELECT value FROM legacy_marker WHERE id = 1"))).scalar_one()

        assert schema["tables"] == {"legacy_marker", "long_term_memory_embedding_selection_token"}
        assert {
            "id",
            "uid",
            "profile_id",
            "token_digest",
            "profile_config_digest",
            "active_embedding_revision",
            "target_embedding_channel_id",
            "target_embedding_model_id",
            "target_embedding_dimensions",
            "target_embedding_signature",
            "expires_at",
            "consumed_at",
            "created_at",
        } <= schema["columns"]
        assert schema["foreign_keys"] == []
        assert {
            "ix_ltm_embedding_selection_token_id",
            "ix_ltm_embedding_selection_token_uid",
            "ix_ltm_embedding_selection_token_profile_id",
            "ix_ltm_embedding_selection_token_uid_profile",
            "ix_ltm_embedding_selection_token_expires_at",
            "ix_ltm_embedding_selection_token_consumed_at",
            "ix_ltm_embedding_selection_token_created_at",
        } <= schema["indexes"]
        assert schema["unique"] == {
            ("uq_ltm_embedding_selection_token_digest", ("token_digest",)),
        }
        assert dict(row) == {"uid": "user-a", "profile_id": 11, "token_digest": "a" * 64}
        assert legacy_value == "preserved"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("dialect", [sqlite.dialect(), mysql.dialect()])
def test_selection_token_migration_table_and_indexes_compile_for_supported_dialects(dialect) -> None:
    table = selection_token_migration.long_term_memory_embedding_selection_token

    assert str(CreateTable(table).compile(dialect=dialect)).strip()
    for index in table.indexes:
        assert str(CreateIndex(index).compile(dialect=dialect)).strip()

    identifiers = {table.name, *[column.name for column in table.columns]}
    identifiers.update(index.name for index in table.indexes if index.name)
    identifiers.update(constraint.name for constraint in table.constraints if constraint.name)
    assert max(map(len, identifiers)) <= 63


@pytest.mark.asyncio
async def test_preview_uses_detected_dimensions_stores_only_digest_and_is_user_isolated(db_session: AsyncSession, monkeypatch) -> None:
    profile = await _create_profile(db_session, uid="user-a", name="profile-a")
    profile_id = profile.id
    calls = _patch_embedding_probe(monkeypatch, dimensions=3)

    preview = await embedding_service.preview_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=profile_id,
        embedding_channel_id=7,
        embedding_model_id="embed-v1",
    )
    token = preview["embedding_selection_signature"]

    assert not {key for key in preview if "token" in key.lower()}
    assert {key for key in preview if "signature" in key} == {"embedding_selection_signature"}
    assert preview["channel_id"] == 7
    assert preview["model_id"] == "embed-v1"
    assert preview["dimensions"] == 3
    assert preview["actual_dimensions"] == 3
    assert preview["current_active"]["revision"] == 0
    assert calls[0] == ("load", (7, "embed-v1"))
    assert calls[1][0] == "detect"

    selection = await _get_selection(db_session, uid="user-a", profile_id=profile_id, token=token)
    assert selection is not None
    assert selection.token_digest == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert selection.profile_config_digest == embedding_service._profile_digest(profile)
    assert selection.uid == "user-a"
    assert selection.profile_id == profile_id
    assert selection.active_embedding_revision == 0
    assert selection.target_embedding_channel_id == 7
    assert selection.target_embedding_model_id == "embed-v1"
    assert selection.target_embedding_dimensions == 3
    assert selection.target_embedding_signature == embedding_service.build_embedding_signature(7, "embed-v1", 3)
    now = get_local_time()
    expires_at = selection.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    assert expires_at > now
    assert token not in selection.model_dump().values()
    assert selection.token_digest != token

    with pytest.raises(ResourceNotFoundException):
        await embedding_service.preview_embedding_selection(
            db_session,
            uid="user-b",
            profile_id=profile_id,
            embedding_channel_id=7,
            embedding_model_id="embed-v1",
        )


@pytest.mark.asyncio
async def test_initial_confirm_updates_all_same_uid_profiles_atomically_and_keeps_profile_budgets(db_session: AsyncSession, monkeypatch) -> None:
    first_profile = await _create_profile(
        db_session,
        uid="user-a",
        name="profile-a",
        memory=_memory_config(enabled=False, top_k=7, candidate_k=11, result_max_chars=5000),
    )
    second_profile_id = (
        await _create_profile(
            db_session,
            uid="user-a",
            name="profile-b",
            memory=_memory_config(enabled=True, top_k=3, candidate_k=4, result_max_chars=2500),
        )
    ).id
    _patch_embedding_probe(monkeypatch, dimensions=3)
    preview = await embedding_service.preview_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=first_profile.id,
        embedding_channel_id=7,
        embedding_model_id="embed-v1",
    )
    token = preview["embedding_selection_signature"]

    await embedding_service.confirm_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=first_profile.id,
        memory=_memory_config(enabled=True, top_k=7, candidate_k=11, result_max_chars=5000),
        embedding_selection_signature=token,
    )

    store = await _get_store(db_session, "user-a")
    revisions = list((await db_session.execute(select(LongTermMemoryEmbeddingRevision).where(LongTermMemoryEmbeddingRevision.uid == "user-a"))).scalars().all())
    jobs = list((await db_session.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == "user-a"))).scalars().all())
    selection = await _get_selection(db_session, uid="user-a", profile_id=first_profile.id, token=token)
    profiles = list((await db_session.execute(select(Profile).where(Profile.uid == "user-a").order_by(Profile.id))).scalars().all())

    assert store is not None
    assert store.active_embedding_channel_id == 7
    assert store.active_embedding_model_id == "embed-v1"
    assert store.active_embedding_dimensions == 3
    assert store.active_embedding_signature == embedding_service.build_embedding_signature(7, "embed-v1", 3)
    assert store.active_embedding_revision == 1
    assert store.active_collection_name is not None
    assert store.target_embedding_channel_id is None
    assert store.target_embedding_model_id is None
    assert store.migration_job_id is None
    assert len(jobs) == 0
    assert len(revisions) == 1
    assert revisions[0].revision == 1
    assert revisions[0].from_channel_id is None
    assert revisions[0].from_model_id is None
    assert revisions[0].to_channel_id == 7
    assert revisions[0].to_model_id == "embed-v1"
    assert revisions[0].to_dimensions == 3
    assert revisions[0].to_collection is not None
    assert revisions[0].status == LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED
    assert revisions[0].embedding_selection_signature == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert selection is not None
    assert selection.consumed_at is not None

    first_memory = ProfileConfig.model_validate(profiles[0].configs).memory
    second_memory = ProfileConfig.model_validate(profiles[1].configs).memory
    assert first_memory.enabled is True
    assert first_memory.top_k == 7
    assert first_memory.candidate_k == 11
    assert first_memory.result_max_chars == 5000
    assert second_memory.enabled is True
    assert second_memory.top_k == 3
    assert second_memory.candidate_k == 4
    assert second_memory.result_max_chars == 2500
    assert {first_memory.embedding_channel_id, second_memory.embedding_channel_id} == {7}
    assert {first_memory.embedding_model_id, second_memory.embedding_model_id} == {"embed-v1"}
    assert profiles[1].id == second_profile_id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["replay", "expired", "profile_changed", "active_revision_changed", "other_uid"])
async def test_confirm_rejects_replay_expiry_stale_revision_and_other_uid_without_partial_changes(
    db_session: AsyncSession,
    monkeypatch,
    failure: str,
) -> None:
    profile = await _create_profile(db_session, uid="user-a", name="profile-a")
    profile_id = profile.id
    _patch_embedding_probe(monkeypatch, dimensions=3)
    preview = await embedding_service.preview_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=profile_id,
        embedding_channel_id=7,
        embedding_model_id="embed-v1",
    )
    token = preview["embedding_selection_signature"]

    if failure == "replay":
        await embedding_service.confirm_embedding_selection(
            db_session,
            uid="user-a",
            profile_id=profile_id,
            memory=_memory_config(enabled=True),
            embedding_selection_signature=token,
        )
        expected_error = ERR_PROFILE_MEMORY_SELECTION_INVALID
    elif failure == "expired":
        selection = await _get_selection(db_session, uid="user-a", profile_id=profile_id, token=token)
        assert selection is not None
        selection.expires_at = get_local_time() - timedelta(seconds=1)
        db_session.add(selection)
        await db_session.commit()
        expected_error = ERR_PROFILE_MEMORY_SELECTION_EXPIRED
    elif failure == "profile_changed":
        profile.name = "changed-after-preview"
        db_session.add(profile)
        await db_session.commit()
        expected_error = ERR_PROFILE_MEMORY_SELECTION_STALE
    elif failure == "active_revision_changed":
        await memory_store_crud.create(
            db_session,
            uid="user-a",
            active_embedding_revision=1,
            active_embedding_channel_id=9,
            active_embedding_model_id="old-model",
            active_embedding_dimensions=3,
            active_embedding_signature=embedding_service.build_embedding_signature(9, "old-model", 3),
            active_collection_name="old-collection",
        )
        store = await _get_store(db_session, "user-a")
        assert store is not None
        store.active_embedding_revision = 2
        db_session.add(store)
        await db_session.commit()
        expected_error = ERR_PROFILE_MEMORY_SELECTION_STALE
    else:
        expected_error = None

    with pytest.raises((ParameterException, ResourceNotFoundException)) as exc_info:
        await embedding_service.confirm_embedding_selection(
            db_session,
            uid="user-b" if failure == "other_uid" else "user-a",
            profile_id=profile_id,
            memory=_memory_config(enabled=True),
            embedding_selection_signature=token,
        )

    if expected_error is not None:
        assert exc_info.value.message == expected_error
    else:
        assert isinstance(exc_info.value, ResourceNotFoundException)

    selection = await _get_selection(db_session, uid="user-a", profile_id=profile_id, token=token)
    store = await _get_store(db_session, "user-a")
    revisions = list((await db_session.execute(select(LongTermMemoryEmbeddingRevision).where(LongTermMemoryEmbeddingRevision.uid == "user-a"))).scalars().all())
    jobs = list((await db_session.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == "user-a"))).scalars().all())

    assert selection is not None
    if failure == "replay":
        assert selection.consumed_at is not None
    else:
        assert selection.consumed_at is None
    assert len(revisions) == (1 if failure == "replay" else 0)
    assert len(jobs) == 0
    if failure in {"active_revision_changed", "replay"}:
        assert store is not None
        assert store.active_embedding_revision == (2 if failure == "active_revision_changed" else 1)
    else:
        assert store is None


@pytest.mark.asyncio
async def test_failed_initial_confirm_rolls_back_consumed_token_store_revision_and_profile(db_session: AsyncSession, monkeypatch) -> None:
    profile = await _create_profile(db_session, uid="user-a", name="profile-a")
    profile_id = profile.id
    original_configs = profile.configs.copy()
    _patch_embedding_probe(monkeypatch, dimensions=3)
    preview = await embedding_service.preview_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=profile_id,
        embedding_channel_id=7,
        embedding_model_id="embed-v1",
    )
    token = preview["embedding_selection_signature"]

    async def fail_revision_create(*_args, **_kwargs):
        raise RuntimeError("revision write failed")

    monkeypatch.setattr(embedding_service.memory_embedding_revision_crud, "create", fail_revision_create)
    with pytest.raises(RuntimeError, match="revision write failed"):
        await embedding_service.confirm_embedding_selection(
            db_session,
            uid="user-a",
            profile_id=profile_id,
            memory=_memory_config(enabled=True),
            embedding_selection_signature=token,
        )

    refreshed_profile = await profile_crud.get_snapshot(db_session, profile_id)
    selection = await _get_selection(db_session, uid="user-a", profile_id=profile_id, token=token)
    assert refreshed_profile is not None
    assert refreshed_profile.configs == original_configs
    assert selection is not None
    assert selection.consumed_at is None
    assert await _get_store(db_session, "user-a") is None
    assert list((await db_session.execute(select(LongTermMemoryEmbeddingRevision))).scalars().all()) == []
    assert list((await db_session.execute(select(LongTermMemoryMutationJob))).scalars().all()) == []


async def _seed_active_embedding(
    db: AsyncSession,
    *,
    uid: str = "user-a",
    channel_id: int = 7,
    model_id: str = "embed-a",
    dimensions: int = 3,
    revision: int = 1,
    collection: str = "memory-active-a",
    index_status: LongTermMemoryIndexStatus = LongTermMemoryIndexStatus.READY,
    migration_status: LongTermMemoryMigrationStatus | None = None,
) -> LongTermMemoryStore:
    return await memory_store_crud.create(
        db,
        uid=uid,
        active_embedding_channel_id=channel_id,
        active_embedding_model_id=model_id,
        active_embedding_dimensions=dimensions,
        active_embedding_signature=embedding_service.build_embedding_signature(channel_id, model_id, dimensions),
        active_embedding_revision=revision,
        active_collection_name=collection,
        index_status=index_status,
        migration_status=migration_status,
    )


@pytest.mark.asyncio
async def test_migration_confirm_keeps_active_configuration_and_creates_preparing_job_and_revision(db_session: AsyncSession, monkeypatch) -> None:
    await _seed_active_embedding(db_session)
    await memory_embedding_revision_crud.create(
        db_session,
        uid="user-a",
        revision=1,
        to_channel_id=7,
        to_model_id="embed-a",
        to_dimensions=3,
        to_signature=embedding_service.build_embedding_signature(7, "embed-a", 3),
        to_collection="memory-active-a",
        status=LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED,
    )
    profile_a = await _create_profile(
        db_session,
        uid="user-a",
        name="profile-a",
        memory=_memory_config(enabled=True, embedding_channel_id=7, embedding_model_id="embed-a"),
    )
    profile_b = await _create_profile(
        db_session,
        uid="user-a",
        name="profile-b",
        memory=_memory_config(enabled=False, embedding_channel_id=7, embedding_model_id="embed-a", top_k=8, candidate_k=9),
    )
    profile_b_id = profile_b.id
    _patch_embedding_probe(monkeypatch, dimensions=5)
    preview = await embedding_service.preview_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=profile_a.id,
        embedding_channel_id=8,
        embedding_model_id="embed-b",
    )
    token = preview["embedding_selection_signature"]

    await embedding_service.confirm_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=profile_a.id,
        memory=_memory_config(enabled=True, embedding_channel_id=8, embedding_model_id="embed-b"),
        embedding_selection_signature=token,
    )

    store = await _get_store(db_session, "user-a")
    assert store is not None
    assert store.active_embedding_channel_id == 7
    assert store.active_embedding_model_id == "embed-a"
    assert store.active_embedding_dimensions == 3
    assert store.active_collection_name == "memory-active-a"
    assert store.active_embedding_revision == 1
    assert store.index_status == LongTermMemoryIndexStatus.READY
    assert store.target_embedding_channel_id == 8
    assert store.target_embedding_model_id == "embed-b"
    assert store.target_embedding_dimensions == 5
    assert store.target_embedding_signature == embedding_service.build_embedding_signature(8, "embed-b", 5)
    assert store.target_collection_name is not None
    assert store.migration_status == LongTermMemoryMigrationStatus.PREPARING
    assert store.migration_job_id is not None

    job = await memory_job_crud.get_by_id(db_session, uid="user-a", job_id=store.migration_job_id)
    revision = await memory_embedding_revision_crud.get_by_revision(db_session, uid="user-a", revision=2)
    assert job is not None
    assert job.operation == LongTermMemoryMutationOperation.EMBEDDING_MIGRATION
    assert job.status == LongTermMemoryMutationStatus.PENDING
    assert job.payload["from"] == {
        "channel_id": 7,
        "model_id": "embed-a",
        "dimensions": 3,
        "signature": embedding_service.build_embedding_signature(7, "embed-a", 3),
        "collection": "memory-active-a",
        "revision": 1,
    }
    assert job.payload["target"]["channel_id"] == 8
    assert job.payload["target"]["model_id"] == "embed-b"
    assert job.payload["target"]["dimensions"] == 5
    assert job.payload["target"]["signature"] == store.target_embedding_signature
    assert job.payload["target"]["collection"] == store.target_collection_name
    assert job.payload["target"]["revision"] == 2
    assert revision is not None
    assert revision.from_channel_id == 7
    assert revision.from_model_id == "embed-a"
    assert revision.from_dimensions == 3
    assert revision.from_collection == "memory-active-a"
    assert revision.to_channel_id == 8
    assert revision.to_model_id == "embed-b"
    assert revision.to_dimensions == 5
    assert revision.to_collection == store.target_collection_name
    assert revision.embedding_selection_signature == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert revision.status == LongTermMemoryEmbeddingRevisionStatus.CONFIRMED

    profiles = list((await db_session.execute(select(Profile).where(Profile.uid == "user-a").order_by(Profile.id))).scalars().all())
    for profile in profiles:
        memory = ProfileConfig.model_validate(profile.configs).memory
        assert memory.embedding_channel_id == 7
        assert memory.embedding_model_id == "embed-a"
    assert ProfileConfig.model_validate(profiles[0].configs).memory.enabled is True
    assert ProfileConfig.model_validate(profiles[1].configs).memory.enabled is False
    assert ProfileConfig.model_validate(profiles[1].configs).memory.top_k == 8

    second_preview = await embedding_service.preview_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=profile_b_id,
        embedding_channel_id=9,
        embedding_model_id="embed-c",
    )
    second_token = second_preview["embedding_selection_signature"]
    with pytest.raises(ParameterException) as exc_info:
        await embedding_service.confirm_embedding_selection(
            db_session,
            uid="user-a",
            profile_id=profile_b_id,
            memory=_memory_config(enabled=False, embedding_channel_id=9, embedding_model_id="embed-c"),
            embedding_selection_signature=second_token,
        )
    assert exc_info.value.message == ERR_PROFILE_MEMORY_MIGRATION_ACTIVE
    second_selection = await _get_selection(db_session, uid="user-a", profile_id=profile_b_id, token=second_token)
    assert second_selection is not None
    assert second_selection.consumed_at is None
    assert (await memory_embedding_revision_crud.get_next_revision(db_session, uid="user-a")) == 3


@pytest.mark.asyncio
async def test_migration_confirm_rolls_back_when_store_migration_claim_is_lost(db_session: AsyncSession, monkeypatch) -> None:
    await _seed_active_embedding(db_session)
    await memory_embedding_revision_crud.create(
        db_session,
        uid="user-a",
        revision=1,
        to_channel_id=7,
        to_model_id="embed-a",
        to_dimensions=3,
        to_signature=embedding_service.build_embedding_signature(7, "embed-a", 3),
        to_collection="memory-active-a",
        status=LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED,
    )
    profile = await _create_profile(
        db_session,
        uid="user-a",
        name="profile-a",
        memory=_memory_config(enabled=True, embedding_channel_id=7, embedding_model_id="embed-a"),
    )
    profile_id = profile.id
    _patch_embedding_probe(monkeypatch, dimensions=5)
    preview = await embedding_service.preview_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=profile_id,
        embedding_channel_id=8,
        embedding_model_id="embed-b",
    )
    token = preview["embedding_selection_signature"]

    async def lose_migration_claim(*_args, **_kwargs):
        return None

    monkeypatch.setattr(embedding_service.memory_store_crud, "start_embedding_migration", lose_migration_claim)
    with pytest.raises(ParameterException) as exc_info:
        await embedding_service.confirm_embedding_selection(
            db_session,
            uid="user-a",
            profile_id=profile_id,
            memory=_memory_config(enabled=True, embedding_channel_id=8, embedding_model_id="embed-b"),
            embedding_selection_signature=token,
        )

    assert exc_info.value.message == ERR_PROFILE_MEMORY_MIGRATION_CONFLICT
    selection = await _get_selection(db_session, uid="user-a", profile_id=profile_id, token=token)
    revisions = list((await db_session.execute(select(LongTermMemoryEmbeddingRevision).where(LongTermMemoryEmbeddingRevision.uid == "user-a").order_by(LongTermMemoryEmbeddingRevision.revision))).scalars().all())
    jobs = list((await db_session.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == "user-a"))).scalars().all())
    store = await _get_store(db_session, "user-a")

    assert selection is not None
    assert selection.consumed_at is None
    assert [revision.revision for revision in revisions] == [1]
    assert jobs == []
    assert store is not None
    assert store.active_embedding_revision == 1
    assert store.target_embedding_channel_id is None
    assert store.target_embedding_model_id is None
    assert store.migration_job_id is None


@pytest.mark.asyncio
async def test_migration_confirm_uses_max_history_revision_after_failed_previous_migration(db_session: AsyncSession, monkeypatch) -> None:
    await _seed_active_embedding(db_session, migration_status=LongTermMemoryMigrationStatus.FAILED)
    profile = await _create_profile(
        db_session,
        uid="user-a",
        name="profile-a",
        memory=_memory_config(enabled=True, embedding_channel_id=7, embedding_model_id="embed-a"),
    )
    failed_revision = await memory_embedding_revision_crud.create(
        db_session,
        uid="user-a",
        revision=2,
        from_channel_id=7,
        from_model_id="embed-a",
        from_dimensions=3,
        from_collection="memory-active-a",
        to_channel_id=8,
        to_model_id="embed-b",
        to_dimensions=5,
        to_signature=embedding_service.build_embedding_signature(8, "embed-b", 5),
        to_collection="memory-target-b",
        status=LongTermMemoryEmbeddingRevisionStatus.FAILED,
    )
    assert failed_revision.revision == 2
    _patch_embedding_probe(monkeypatch, dimensions=7)
    preview = await embedding_service.preview_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=profile.id,
        embedding_channel_id=9,
        embedding_model_id="embed-c",
    )
    token = preview["embedding_selection_signature"]

    await embedding_service.confirm_embedding_selection(
        db_session,
        uid="user-a",
        profile_id=profile.id,
        memory=_memory_config(enabled=True, embedding_channel_id=9, embedding_model_id="embed-c"),
        embedding_selection_signature=token,
    )

    revision = await memory_embedding_revision_crud.get_by_revision(db_session, uid="user-a", revision=3)
    store = await _get_store(db_session, "user-a")
    assert revision is not None
    assert revision.revision == 3
    assert revision.to_channel_id == 9
    assert revision.to_model_id == "embed-c"
    assert revision.to_dimensions == 7
    assert revision.job_id is not None
    assert store is not None
    assert store.active_embedding_revision == 1
    assert store.target_embedding_channel_id == 9
    job = await memory_job_crud.get_by_id(db_session, uid="user-a", job_id=revision.job_id)
    assert job is not None
    assert job.payload["target"]["revision"] == 3


@pytest.mark.parametrize("normalizer_name", ["create", "update"])
def test_normalize_profile_memory_without_active_store_requires_disabled_and_empty_selection(normalizer_name: str) -> None:
    normalizer = getattr(embedding_service, f"normalize_profile_memory_for_{normalizer_name}")
    profile = Profile(id=1, uid="user-a", name="profile-a", configs=_profile_configs())

    normalized = normalizer(_profile_configs(_memory_config(enabled=False)), None) if normalizer_name == "create" else normalizer(profile, _profile_configs(_memory_config(enabled=False)), None)
    memory = ProfileConfig.model_validate(normalized).memory
    assert memory.enabled is False
    assert memory.embedding_channel_id is None
    assert memory.embedding_model_id is None

    for invalid_memory in (
        _memory_config(enabled=True),
        _memory_config(embedding_channel_id=7, embedding_model_id="embed-v1"),
    ):
        with pytest.raises(ParameterException) as exc_info:
            if normalizer_name == "create":
                normalizer(_profile_configs(invalid_memory), None)
            else:
                normalizer(profile, _profile_configs(invalid_memory), None)
        assert exc_info.value.message in {
            ERR_PROFILE_MEMORY_ACTIVE_NOT_CONFIGURED,
            ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED,
        }


@pytest.mark.parametrize("normalizer_name", ["create", "update"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("selection", ["empty", "active"])
def test_normalize_profile_memory_with_active_store_preserves_enabled_and_normalizes_selection(
    normalizer_name: str,
    enabled: bool,
    selection: str,
) -> None:
    store = LongTermMemoryStore(
        uid="user-a",
        active_embedding_channel_id=7,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=3,
        active_embedding_signature=embedding_service.build_embedding_signature(7, "embed-v1", 3),
        active_embedding_revision=1,
        active_collection_name="active-collection",
    )
    profile = Profile(id=1, uid="user-a", name="profile-a", configs=_profile_configs())
    normalizer = getattr(embedding_service, f"normalize_profile_memory_for_{normalizer_name}")
    memory = _memory_config(
        enabled=enabled,
        embedding_channel_id=7 if selection == "active" else None,
        embedding_model_id="embed-v1" if selection == "active" else None,
        top_k=8,
        candidate_k=9,
    )
    normalized = normalizer(_profile_configs(memory), store) if normalizer_name == "create" else normalizer(profile, _profile_configs(memory), store)
    result = ProfileConfig.model_validate(normalized).memory

    assert result.enabled is enabled
    assert result.top_k == 8
    assert result.candidate_k == 9
    assert result.embedding_channel_id == 7
    assert result.embedding_model_id == "embed-v1"


@pytest.mark.parametrize("normalizer_name", ["create", "update"])
def test_normalize_profile_memory_rejects_a_different_target_when_active_exists(normalizer_name: str) -> None:
    store = LongTermMemoryStore(
        uid="user-a",
        active_embedding_channel_id=7,
        active_embedding_model_id="embed-v1",
        active_embedding_dimensions=3,
        active_embedding_signature=embedding_service.build_embedding_signature(7, "embed-v1", 3),
        active_embedding_revision=1,
        active_collection_name="active-collection",
    )
    profile = Profile(id=1, uid="user-a", name="profile-a", configs=_profile_configs())
    normalizer = getattr(embedding_service, f"normalize_profile_memory_for_{normalizer_name}")
    different_memory = _memory_config(embedding_channel_id=8, embedding_model_id="embed-v2")

    with pytest.raises(ParameterException) as exc_info:
        if normalizer_name == "create":
            normalizer(_profile_configs(different_memory), store)
        else:
            normalizer(profile, _profile_configs(different_memory), store)
    assert exc_info.value.message == ERR_PROFILE_MEMORY_CONFIRMATION_REQUIRED
