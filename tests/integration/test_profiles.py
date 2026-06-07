import pytest
from httpx import ASGITransport, AsyncClient

from sqlmodel import SQLModel
import app.models  # 导入所有模型以注册 metadata
from app.providers.database import engine
from main import app


@pytest.fixture(scope="module", autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
        await conn.run_sync(SQLModel.metadata.create_all)
    yield


@pytest.mark.asyncio
async def test_profile_management_full_flow():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        await ac.post(
            "/api/v1/auth/reset_admin",
            json={"reset_token": "ed126d6c5a4ea6bf33774214633d2a16"},
        )
        login_resp = await ac.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        print("Login Resp:", login_resp.status_code, login_resp.text)
        token = login_resp.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 1. 创建 Provider
        # 确保 provider_type 与 Enum 匹配 (OPENAI)
        provider_resp = await ac.post(
            "/api/v1/providers/create",
            json={
                "name": "P1",
                "api_key": "sk-test",
                "base_url": "http://api.openai.com/v1",
                "provider_type": "OPENAI",
                "is_active": True,
            },
            headers=headers,
        )
        assert provider_resp.status_code == 200
        provider_id = provider_resp.json()["data"]["id"]

        # 2. 创建 Profile
        profile_data = {
            "name": "C1",
            "configs": {
                "provider": {
                    "provider_id": provider_id,
                    "model_id": "gpt-3.5-turbo",
                    "temperature": 0.7,
                    "top_p": 1.0,
                    "max_tokens": 2048,
                    "stream": False,
                    "context_window_k": 4,
                },
                "security": {
                    "audit_threshold": 5,
                    "audit_provider_id": provider_id,
                    "audit_model_id": "gpt-3.5-turbo",
                },
                "tool": {"shell_timeout": 30.0},
                "other": {},
            },
        }
        resp = await ac.post("/api/v1/profiles/create", json=profile_data, headers=headers)
        assert resp.status_code == 200
        profile_id = resp.json()["data"]["id"]

        # 3. 激活
        resp = await ac.post(
            "/api/v1/profiles/activate",
            params={"profile_id": profile_id},
            headers=headers,
        )
        assert resp.status_code == 200
