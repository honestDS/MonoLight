import pytest
import os
import time
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
async def test_chat_advanced_logic():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        await ac.post("/api/v1/auth/reset_admin", json={"reset_token": "ed126d6c5a4ea6bf33774214633d2a16"})
        login_resp = await ac.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        token = login_resp.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # --- 422 类型错误验证：message 传入数字而非字符串 ---
        r_chat_type = await ac.post("/api/v1/chat/completions", json={"message": 12345}, headers=headers)
        assert r_chat_type.status_code == 422

        # --- 422 类型错误验证：stream 传入非布尔值 ---
        r_stream_type = await ac.post("/api/v1/chat/completions", json={"message": "hi", "stream": "yes_please"}, headers=headers)
        assert r_stream_type.status_code == 422
