from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.models.memory import LongTermMemoryStore
from app.models.profile import LongTermMemoryConfig, Profile, ProfileConfig
from scripts import migration_20260820_sync_profile_memory_embedding as migration

MEMORY_TABLES = (Profile.__table__, LongTermMemoryStore.__table__)


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


def _profile_configs(memory: LongTermMemoryConfig) -> dict:
    return ProfileConfig.model_validate({"memory": memory.model_dump()}).model_dump()


@pytest_asyncio.fixture
async def database(tmp_path) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'profile-memory-embedding-sync.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=MEMORY_TABLES))

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _memory_snapshots(session: AsyncSession) -> dict[str, dict]:
    result = await session.execute(select(Profile).order_by(Profile.id).execution_options(populate_existing=True))
    return {profile.name: ProfileConfig.model_validate(profile.configs).memory.model_dump() for profile in result.scalars().all()}


@pytest.mark.asyncio
async def test_sync_profile_memory_embedding_migration_updates_only_configured_users_and_is_idempotent(
    database: async_sessionmaker[AsyncSession],
) -> None:
    configured_uid = "configured-user"
    skipped_uid = "skipped-user"
    active_channel_id = 73
    active_model_id = "embedding-active-v2"

    configured_memory_a = _memory_config(
        enabled=True,
        embedding_channel_id=11,
        embedding_model_id="embedding-old-a",
        top_k=7,
        candidate_k=12,
        result_max_chars=8192,
    )
    configured_memory_b = _memory_config(
        enabled=False,
        embedding_channel_id=12,
        embedding_model_id="embedding-old-b",
        top_k=3,
        candidate_k=9,
        result_max_chars=2048,
    )
    skipped_memory = _memory_config(
        enabled=True,
        embedding_channel_id=99,
        embedding_model_id="embedding-skipped",
        top_k=8,
        candidate_k=16,
        result_max_chars=6000,
    )

    async with database() as session:
        session.add_all(
            [
                LongTermMemoryStore(
                    uid=configured_uid,
                    active_embedding_channel_id=active_channel_id,
                    active_embedding_model_id=active_model_id,
                    active_embedding_dimensions=1536,
                    active_embedding_signature="active-signature",
                    active_embedding_revision=4,
                    active_collection_name="configured-memory-collection",
                ),
                LongTermMemoryStore(
                    uid=skipped_uid,
                    active_embedding_channel_id=active_channel_id,
                    active_embedding_model_id=active_model_id,
                    active_embedding_dimensions=1536,
                    active_embedding_signature="skipped-signature",
                    active_embedding_revision=0,
                    active_collection_name="skipped-memory-collection",
                ),
                Profile(
                    uid=configured_uid,
                    name="configured-profile-a",
                    configs=_profile_configs(configured_memory_a),
                ),
                Profile(
                    uid=configured_uid,
                    name="configured-profile-b",
                    configs=_profile_configs(configured_memory_b),
                ),
                Profile(
                    uid=skipped_uid,
                    name="skipped-profile",
                    configs=_profile_configs(skipped_memory),
                ),
            ]
        )
        await session.commit()

        await migration.migrate(session)
        await session.commit()
        first_snapshot = await _memory_snapshots(session)

        await migration.migrate(session)
        await session.commit()
        second_snapshot = await _memory_snapshots(session)

        store = (await session.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == configured_uid))).scalar_one()

    assert migration.MIGRATION_ID == "20260820_sync_profile_memory_embedding_v1"
    assert second_snapshot == first_snapshot

    configured_originals = {
        "configured-profile-a": configured_memory_a.model_dump(),
        "configured-profile-b": configured_memory_b.model_dump(),
    }
    for profile_name, original_memory in configured_originals.items():
        migrated_memory = first_snapshot[profile_name]
        assert migrated_memory["embedding_channel_id"] == store.active_embedding_channel_id
        assert migrated_memory["embedding_model_id"] == store.active_embedding_model_id
        for field in ("enabled", "top_k", "candidate_k", "result_max_chars"):
            assert migrated_memory[field] == original_memory[field]

    assert first_snapshot["skipped-profile"] == skipped_memory.model_dump()
