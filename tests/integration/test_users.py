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
async def test_user_management_flow():
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

        # 422 验证：缺失必填字段 username
        r_invalid = await ac.post(
            "/api/v1/admin/user/add",
            json={"password": "p1"},
            headers=headers,
        )
        assert r_invalid.status_code == 422

        # 422 验证：uid 传入非字符串类型
        r_up_type = await ac.post(
            "/api/v1/admin/user/update",
            json={"uid": 12345, "is_active": True},
            headers=headers,
        )
        assert r_up_type.status_code == 422
