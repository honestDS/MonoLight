import json
import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import ASGITransport, AsyncClient
from main import app
from app.providers.database import engine, Base, get_db
from app.models.message import Message
from sqlalchemy import select
from app.core.dispatcher import CONFIRMATION_TOKEN

@pytest.fixture(scope="module", autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield

@pytest.mark.asyncio
async def test_full_chat_audit_flow():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. 认证初始化
        await ac.post("/api/v1/auth/reset_admin", json={"reset_token": "ed126d6c5a4ea6bf33774214633d2a16"})
        login_resp = await ac.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        token = login_resp.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 2. 创建 Provider
        provider_data = {
            "name": "TestProvider",
            "api_key": "sk-test",
            "base_url": "http://test.api",
            "provider_type": "OPENAI",
            "is_active": True
        }
        await ac.post("/api/v1/providers/create", json=provider_data, headers=headers)

        # 3. 创建 Profile
        profile_data = {
            "name": "ChatProfile",
            "provider_id": 1,
            "model_id": "gpt-4",
            "is_active": True
        }
        await ac.post("/api/v1/profiles/create", json=profile_data, headers=headers)
        
        # 4. 显式激活 Profile (确保数据库 is_active=1)
        await ac.post("/api/v1/profiles/activate", params={"profile_id": 1}, headers=headers)

        # 5. 模拟对话流程
        mock_llm_call = {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "execute_shell", "arguments": '{"command":"rm risky_file"}'}}]}}]}
        mock_audit_res = {"score": 6, "reason": "Potentially risky command"}
        mock_llm_final = {"choices": [{"message": {"role": "assistant", "content": "Command requires confirmation."}}]}
        
        with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(side_effect=[mock_llm_call, mock_llm_final])):
            with patch("app.core.dispatcher.audit_command", AsyncMock(return_value=mock_audit_res)):
                resp = await ac.post("/api/v1/chat/completions", json={"message": "delete it"}, headers=headers)
                assert resp.status_code == 200
                assert "confirmation" in resp.json()["choices"][0]["message"]["content"]

        # 6. 带令牌确认执行
        confirm_command = f"{CONFIRMATION_TOKEN} rm risky_file"
        with patch("app.core.dispatcher.ShellExecutor.execute", AsyncMock(return_value=json.dumps({"stdout": "deleted", "exit_code": 0}))) as mock_shell:
            with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(side_effect=[mock_llm_call, {"choices": [{"message": {"role": "assistant", "content": "Task completed."}}]}])):
                resp = await ac.post("/api/v1/chat/completions", json={"message": confirm_command}, headers=headers)
                assert resp.status_code == 200
                assert "Task completed" in resp.json()["choices"][0]["message"]["content"]
                mock_shell.assert_called_once()
                assert mock_shell.call_args[0][0] == "rm risky_file"

@pytest.mark.asyncio
async def test_chat_validation_errors():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        login_resp = await ac.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
        token = login_resp.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        r = await ac.post("/api/v1/chat/completions", json={"message": 123}, headers=headers)
        assert r.status_code == 422
