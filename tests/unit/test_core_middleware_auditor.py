import json
import pytest
from unittest.mock import AsyncMock, patch
from app.core.middleware.auditor import audit_command


@pytest.mark.asyncio
async def test_audit_command_success():
    """测试审计指令成功场景：正确解析 LLMClient 的返回"""
    mock_llm_response = {
        "choices": [
            {"message": {"content": json.dumps({"score": 2, "reason": "Safe command"})}}
        ]
    }
    # 由于 auditor.py 已经重构为复用 LLMClient.generate，这里直接 mock 该方法
    with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(return_value=mock_llm_response)):
        result = await audit_command("ls", "http://test.api", "key", "model-1")
        assert result["score"] == 2
        assert result["reason"] == "Safe command"


@pytest.mark.asyncio
async def test_audit_command_markdown_json_success():
    """测试审计指令成功场景：LLM 返回包含 Markdown 块的情况"""
    mock_content = "```json\n{\"score\": 0, \"reason\": \"Verified\"}\n```"
    mock_llm_response = {
        "choices": [
            {"message": {"content": mock_content}}
        ]
    }
    with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(return_value=mock_llm_response)):
        result = await audit_command("ls", "http://test.api", "key", "model-1")
        assert result["score"] == 0
        assert result["reason"] == "Verified"


@pytest.mark.asyncio
async def test_audit_command_error_handling():
    """测试审计指令失败场景：LLMClient 抛出异常或返回无效内容"""
    # 模拟网络异常或 API 错误导致的 LLMClient 失败
    with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(side_effect=Exception("API Error"))):
        result = await audit_command("ls", "http://test.api", "key", "model-1")
        assert result is None


@pytest.mark.asyncio
async def test_audit_command_invalid_content_json():
    """测试审计指令失败场景：LLM 返回的内容不是合法的 JSON 格式"""
    mock_llm_response = {
        "choices": [
            {"message": {"content": "Not a JSON output at all"}}
        ]
    }
    with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(return_value=mock_llm_response)):
        result = await audit_command("ls", "http://test.api", "key", "model-1")
        assert result is None
