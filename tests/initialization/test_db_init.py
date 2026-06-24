import pytest
from sqlalchemy import select

from app.models.profile import Profile
from app.providers.database import Base, engine


@pytest.mark.asyncio
async def test_default_profile_initialization_with_timeout(db_session):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    check = await db_session.execute(select(Profile).where(Profile.name == "default"))
    profile = check.scalars().first()

    if not profile:
        default_profile = Profile(
            name="default",
            provider_id=-1,
            configs={
                "provider": {
                    "model_id": "test-model",
                    "temperature": 0.7,
                    "top_p": 1.0,
                    "max_tokens": 2048,
                    "stream": False,
                },
                "security": {"audit_threshold": 5},
                "tool": {"tool_timeout": 30.0},
                "other": {"context_window_k": 4},
            },
            is_active=True,
        )
        db_session.add(default_profile)
        await db_session.commit()
        await db_session.refresh(default_profile)
        profile = default_profile

    assert profile.configs is not None
    # 兼容性检查：确保 configs 是 dict 且包含 tool.tool_timeout
    configs = profile.configs
    if isinstance(configs, str):
        import json

        configs = json.loads(configs)
    assert configs["tool"]["tool_timeout"] == 30.0
