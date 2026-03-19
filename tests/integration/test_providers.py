import pytest
import os
from httpx import ASGITransport, AsyncClient
from main import app
from app.providers.database import engine, Base


@pytest.fixture(scope="module", autouse=True)
async def setup_db():
    os.environ["ADMIN_RESET_TOKEN"] = "ed126d6c5a4ea6bf33774214633d2a16"
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.mark.asyncio
async def test_provider_full_lifecycle():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        await ac.post(
            "/api/v1/auth/reset_admin",
            json={"reset_token": "ed126d6c5a4ea6bf33774214633d2a16"},
        )
        login_resp = await ac.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
        )
        token = login_resp.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # --- 非法输入验证：无效的枚举值 (ProviderType) ---
        r_err_type = await ac.post(
            "/api/v1/providers/create",
            json={"name": "P1", "provider_type": "INVALID", "api_key": "k"},
            headers=headers,
        )
        assert r_err_type.status_code == 422

        # --- 非法输入验证：缺失必填字段 ---
        r_err_miss = await ac.post(
            "/api/v1/providers/create", json={"name": "P2"}, headers=headers
        )
        assert r_err_miss.status_code == 422

        # 1. 正常创建
        r_ok = await ac.post(
            "/api/v1/providers/create",
            json={"name": "P1", "provider_type": "OPENAI", "api_key": "k1"},
            headers=headers,
        )
        assert r_ok.status_code == 200
