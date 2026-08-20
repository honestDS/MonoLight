from copy import deepcopy

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

from app.models.profile import Profile
from scripts import migration_20260820_add_profile_recall_settings as migration


async def _configs_snapshot(session: AsyncSession) -> dict[str, dict]:
    result = await session.execute(select(Profile).order_by(Profile.id))
    return {profile.name: deepcopy(profile.configs) for profile in result.scalars().all()}


@pytest.mark.asyncio
async def test_profile_recall_settings_migration_is_idempotent_and_preserves_explicit_values(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'profile-recall-settings-migration.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync_connection: Profile.__table__.create(sync_connection))

        legacy_configs = {
            "memory": {
                "enabled": True,
                "top_k": 17,
                "candidate_k": 20,
                "result_max_chars": 12000,
            },
            "channel": {
                "rerank_channel": {
                    "kb_query_top_k": 11,
                    "rerank_candidate_k": 13,
                    "rerank_timeout": 22.5,
                    "rules": [
                        {"channel_id": 1, "model_id": "rerank-model", "priority": 1, "weight": 100},
                    ],
                },
            },
        }
        explicit_configs = {
            "memory": {
                "chat_history": {"top_k": 8, "candidate_k": 16, "result_max_chars": 6000},
                "knowledge": {"top_k": 9, "candidate_k": 14, "result_max_chars": 7000},
            },
            "channel": {
                "rerank_channel": {
                    "kb_query_top_k": 31,
                    "rerank_candidate_k": 2,
                    "rerank_timeout": 18.0,
                    "rules": [
                        {"channel_id": 2, "model_id": "explicit-rerank-model", "priority": 1, "weight": 100},
                    ],
                },
            },
        }

        async with session_factory() as session:
            session.add_all(
                [
                    Profile(uid="legacy-user", name="legacy-profile", configs=legacy_configs),
                    Profile(uid="explicit-user", name="explicit-profile", configs=explicit_configs),
                ]
            )
            await session.commit()

            await migration.migrate(session)
            await session.commit()
            first_snapshot = await _configs_snapshot(session)

            await migration.migrate(session)
            await session.commit()
            second_snapshot = await _configs_snapshot(session)

        assert migration.MIGRATION_ID == "20260820_add_profile_recall_settings_v1"
        assert second_snapshot == first_snapshot

        legacy_memory = first_snapshot["legacy-profile"]["memory"]
        assert legacy_memory["enabled"] is True
        assert legacy_memory["top_k"] == 17
        assert legacy_memory["candidate_k"] == 20
        assert legacy_memory["result_max_chars"] == 12000
        assert legacy_memory["chat_history"] == {"top_k": 17, "candidate_k": 500, "result_max_chars": 12000}
        assert legacy_memory["knowledge"] == {"top_k": 11, "candidate_k": 13, "result_max_chars": 4000}

        legacy_rerank = first_snapshot["legacy-profile"]["channel"]["rerank_channel"]
        assert legacy_rerank["rerank_timeout"] == 22.5
        assert legacy_rerank["rules"] == [
            {"channel_id": 1, "model_id": "rerank-model", "priority": 1, "weight": 100, "is_enabled": True},
        ]
        assert "kb_query_top_k" not in legacy_rerank
        assert "rerank_candidate_k" not in legacy_rerank

        explicit_memory = first_snapshot["explicit-profile"]["memory"]
        assert explicit_memory["chat_history"] == {"top_k": 8, "candidate_k": 16, "result_max_chars": 6000}
        assert explicit_memory["knowledge"] == {"top_k": 9, "candidate_k": 14, "result_max_chars": 7000}
        assert explicit_memory["knowledge"] != {"top_k": 31, "candidate_k": 31, "result_max_chars": 4000}
    finally:
        await engine.dispose()
