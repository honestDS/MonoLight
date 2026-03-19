import pytest
import os
from httpx import ASGITransport, AsyncClient
from main import app
from app.providers.database import engine, Base
from app.core import constants

@pytest.fixture(scope="module", autouse=True)
async def setup_db():
    os.environ["ADMIN_RESET_TOKEN"] = "ed126d6c5a4ea6bf33774214633d2a16"
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield

@pytest.mark.asyncio
async def test_auth_management_flow():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # --- 422 类型错误验证：登录参数不是字符串 ---
        r_login_type = await ac.post("/api/v1/auth/login", json={"username": 123, "password": ["secret"]})
        assert r_login_type.status_code == 422

        # 1. 正常流：正确 Token 重置
        await ac.post("/api/v1/auth/reset_admin", json={"reset_token": "ed126d6c5a4ea6bf33774214633d2a16"})

        # 2. 正常流：登录获取 Token
        r_login_ok = await ac.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        assert r_login_ok.status_code == 200
