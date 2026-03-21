import pytest
from httpx import ASGITransport, AsyncClient
from main import app
from app.providers.database import engine, Base

@pytest.fixture(scope="module", autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield

@pytest.mark.asyncio
async def test_profile_management_full_flow():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Login
        await ac.post("/api/v1/auth/reset_admin", json={"reset_token": "ed126d6c5a4ea6bf33774214633d2a16"})
        login_resp = await ac.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        token = login_resp.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. Create Provider
        provider_data = {
            "name": "TestProvider", 
            "api_key": "sk-test", 
            "base_url": "http://test.api",
            "provider_type": "OPENAI",
            "is_active": True
        }
        p_resp = await ac.post("/api/v1/providers/create", json=provider_data, headers=headers)
        assert p_resp.status_code == 200
        provider_id = p_resp.json()["data"]["id"]

        # 3. Create Profile
        profile_data = {
            "name": "AuditProfile",
            "provider_id": provider_id,
            "model_id": "gpt-4",
            "audit_provider_id": provider_id,
            "audit_model_id": "gpt-4-audit",
            "audit_threshold": 5,
            "is_active": True
        }
        resp = await ac.post("/api/v1/profiles/create", json=profile_data, headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data is not None
        assert data["audit_provider_id"] == provider_id
