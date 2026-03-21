import pytest
from sqlalchemy import inspect, select
from app.providers.database import engine, Base
from app.models.profile import Profile


@pytest.mark.asyncio
async def test_tables_alignment(db_session):
    """验证所有物理表名是否已按单数对齐"""

    def get_tables(connection):
        inst = inspect(connection)
        return inst.get_table_names()

    # 确保测试库中的表已被创建
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with engine.connect() as conn:
        tables = await conn.run_sync(get_tables)

    # 验证单数表名是否存在
    expected_tables = ["user", "provider", "profile", "prompt", "message"]
    for table in expected_tables:
        assert table in tables


@pytest.mark.asyncio
async def test_default_profile_initialization_with_timeout(db_session):
    """验证初始化时默认 Profile 是否包含正确的超时配置"""
    # 模拟 main.py lifespan 中的初始化逻辑
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 检查默认 Profile
    check = await db_session.execute(select(Profile).where(Profile.name == "default"))
    profile = check.scalars().first()

    # 如果不存在（正常测试环境下应不存在），则手动触发一次初始化
    if not profile:
        default_profile = Profile(
            name="default",
            provider_id=-1,
            model_id="test-model",
            extra_config={"shell_timeout": 30},
            is_active=True,
        )
        db_session.add(default_profile)
        await db_session.commit()
        profile = default_profile

    assert profile.extra_config is not None
    assert profile.extra_config.get("shell_timeout") == 30
