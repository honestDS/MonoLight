import pytest
from unittest.mock import AsyncMock, patch
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
async def test_full_chat_audit_flow():
    # 模拟 ChatDispatcher.dispatch 的返回结构，需匹配 app/core/dispatcher.py 的真实返回
    mock_dispatch_res = {
        "choices": [{"message": {"role": "assistant", "content": "Audit Passed"}}]
    }

    with patch(
        "app.core.dispatcher.ChatDispatcher.dispatch",
        AsyncMock(return_value=mock_dispatch_res),
    ):
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

            resp = await ac.post(
                "/api/v1/chat/completions", json={"message": "ls"}, headers=headers
            )

            assert resp.status_code == 200
            data = resp.json()
            # 根据 app/api/v1/chat.py: chat_completions 直接返回 dispatcher 的结果
            assert data["choices"][0]["message"]["content"] == "Audit Passed"
