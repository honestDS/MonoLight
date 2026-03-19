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
async def test_profile_management_full_flow():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        await ac.post("/api/v1/auth/reset_admin", json={"reset_token": "ed126d6c5a4ea6bf33774214633d2a16"})
        login_resp = await ac.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        token = login_resp.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        r_p = await ac.post("/api/v1/providers/create", json={"name": "P1", "provider_type": "OPENAI", "api_key": "k"}, headers=headers)
        provider_id = r_p.json()["data"]["id"]

        # 1. 正常创建
        r_c1 = await ac.post("/api/v1/profiles/create", json={"name": "Prof1", "provider_id": provider_id, "model_id": "m1"}, headers=headers)
        profile_id = r_c1.json()["data"]["id"]

        # --- 非法输入验证：update 接口传入字符串作为 float (应触发 422) ---
        r_up_str = await ac.post("/api/v1/profiles/update", params={"profile_id": profile_id}, json={"temperature": "very_hot"}, headers=headers)
        assert r_up_str.status_code == 422

        # --- 非法输入验证：update 接口传入越界数值 ---
        r_up_out = await ac.post("/api/v1/profiles/update", params={"profile_id": profile_id}, json={"temperature": 2.5}, headers=headers)
        assert r_up_out.status_code == 422

        # 2. 正常更新
        r_up_ok = await ac.post("/api/v1/profiles/update", params={"profile_id": profile_id}, json={"temperature": 1.5}, headers=headers)
        assert r_up_ok.status_code == 200
        assert r_up_ok.json()["data"]["temperature"] == 1.5
