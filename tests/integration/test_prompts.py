import os

import pytest
from httpx import ASGITransport, AsyncClient

from app.providers.database import Base, engine
from main import app


@pytest.fixture(scope="module", autouse=True)
async def setup_db():
    os.environ["ADMIN_RESET_TOKEN"] = "ed126d6c5a4ea6bf33774214633d2a16"
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.mark.asyncio
async def test_prompts_management_full_flow():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        await ac.post(
            "/api/v1/auth/reset_admin",
            json={"reset_token": "ed126d6c5a4ea6bf33774214633d2a16"},
        )
        login_resp = await ac.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        token = login_resp.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # --- 422 类型错误验证：content 传入字典而非字符串 ---
        r_c_type = await ac.post(
            "/api/v1/prompts/create",
            json={"name": "p1", "content": {"text": "hello"}},
            headers=headers,
        )
        assert r_c_type.status_code == 422

        # 1. 正常创建
        r_ok = await ac.post(
            "/api/v1/prompts/create",
            json={"name": "p1", "content": "hi"},
            headers=headers,
        )
        r_ok.json()["data"]["id"]

        # --- 422 类型错误验证：update 接口 prompt_id 类型错误 ---
        r_up_type = await ac.post(
            "/api/v1/prompts/update",
            params={"prompt_id": "not_an_id"},
            json={"name": "new"},
            headers=headers,
        )
        assert r_up_type.status_code == 422
