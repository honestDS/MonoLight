import os
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel as Base

from app.models.profile import Profile
from app.models.channel import ModelChannel


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    """全局测试环境初始化：设置管理令牌环境变量"""
    os.environ["ADMIN_RESET_TOKEN"] = "ed126d6c5a4ea6bf33774214633d2a16"
    yield


@pytest.fixture(autouse=True)
def block_real_shell(monkeypatch):
    """【最终物理防线】全局自动执行：严禁任何测试调用真实 Shell 接口"""
    forbidden = MagicMock(side_effect=RuntimeError("REAL_SHELL_FORBIDDEN_GLOBALLY_IN_TESTS"))
    monkeypatch.setattr("app.core.tools.shell.asyncio.create_subprocess_shell", forbidden)
    monkeypatch.setattr("app.core.tools.shell.asyncio.wait_for", forbidden)


@pytest.fixture
async def db_session():
    """内存数据库会话夹具"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def mock_audit_profile():
    """带有审计配置的 Profile Mock 夹具"""
    profile = MagicMock(spec=Profile)
    profile.id = 999
    profile.is_active = True
    profile.model_id = "gpt-4"
    profile.temperature = 0.7
    profile.max_tokens = 1000
    profile.audit_provider_id = 100
    profile.audit_model_id = "gpt-4-audit"
    profile.audit_threshold = 5

    # 关联 Mock Channel
    channel = MagicMock(spec=ModelChannel)
    channel.id = 100
    channel.api_key = "audit-sk-test"
    channel.base_url = "http://audit-api.test"
    profile.channel = channel
    profile.prompt = None
    return profile


@pytest.fixture
def mock_audit_channel():
    """审计渠道 Mock 夹具"""
    channel = MagicMock(spec=ModelChannel)
    channel.id = 100
    channel.api_key = "audit-sk-test"
    channel.base_url = "http://audit-api.test"
    return channel
