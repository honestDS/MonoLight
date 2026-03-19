import pytest
import os
import tempfile
from httpx import ASGITransport, AsyncClient
from main import app
from app.providers.database import engine, Base

@pytest.fixture(scope="session", autouse=True)
async def setup_test_db():
    # 强制在隔离的测试库中创建所有表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    # 测试结束后的清理逻辑 (可选)

@pytest.mark.asyncio
async def test_isolated_integration_v1():
    os.environ["ADMIN_RESET_TOKEN"] = "ed126d6c5a4ea6bf33774214633d2a16"
    # 这里的 engine 已经由 app/providers/database.py 自动切换到了 /tmp 目录下的临时库
    reset_token = "ed126d6c5a4ea6bf33774214633d2a16"
    
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. 隔离库中的重置逻辑
        r1 = await ac.post("/api/v1/auth/reset_admin", json={"reset_token": reset_token})
        assert r1.status_code == 200, f"Reset Failed: {r1.text}"
        
        # 2. 隔离库中的登录逻辑
        r2 = await ac.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        assert r2.status_code == 200, f"Login Failed: {r2.text}"
        token = r2.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        
        # 3. 隔离库中的 API 探通性
        # 此时数据库中没有任何数据，应返回空列表而非 500
        endpoints = [
            "/api/v1/admin/user/list",
            "/api/v1/providers/list",
            "/api/v1/chat/sessions/list",
            "/api/v1/prompts/list"
        ]
        
        for url in endpoints:
            resp = await ac.get(url, headers=headers)
            assert resp.status_code == 200, f"Endpoint {url} returned {resp.status_code}"
            assert resp.json()["code"] == 200
