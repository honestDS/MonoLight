import json
from unittest.mock import patch
import aiohttp
import pytest
from app.core import constants
from app.core.exceptions import LLMException
from app.providers.llm.client import LLMClient

# 彻底摆脱 unittest.mock，改用纯 Python 异步模拟对象
# AsyncMock 在 Python 3.13 极易产生内部未等待协程警告
class MockResponse:
    def __init__(self, status=200, text_data="{}"):
        self.status = status
        self.text_data = text_data
        
    async def text(self):
        return self.text_data
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

class MockSession:
    def __init__(self, response=None, raise_error=None):
        self.response = response
        self.raise_error = raise_error
        
    def post(self, *args, **kwargs):
        if self.raise_error and not isinstance(self.raise_error, aiohttp.ClientConnectorError):
            raise self.raise_error
        return self.response
        
    async def __aenter__(self):
        if self.raise_error and isinstance(self.raise_error, aiohttp.ClientConnectorError):
            raise self.raise_error
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

# 重点修复：使用普通的闭包/工厂函数替代 patch 的默认 behavior
def get_mock_session_factory(response=None, raise_error=None):
    def factory(*args, **kwargs):
        return MockSession(response=response, raise_error=raise_error)
    return factory

@pytest.mark.asyncio
async def test_generate_success():
    mock_data = {"choices": [{"message": {"role": "assistant", "content": "hello world"}}]}
    mock_response = MockResponse(status=200, text_data=json.dumps(mock_data))
    
    # 彻底禁用 patch 的自动 mock 功能，改为手动注入工厂函数
    with patch("aiohttp.ClientSession", side_effect=get_mock_session_factory(response=mock_response)):
        result = await LLMClient.generate(
            api_key="sk-test",
            base_url="https://api.test.com/v1",
            model_id="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "hi"}]
        )
        assert result["choices"][0]["message"]["content"] == "hello world"

@pytest.mark.asyncio
async def test_generate_http_error():
    mock_response = MockResponse(status=401, text_data="Unauthorized")
    
    with patch("aiohttp.ClientSession", side_effect=get_mock_session_factory(response=mock_response)):
        with pytest.raises(LLMException) as excinfo:
            await LLMClient.generate(
                api_key="invalid",
                base_url="https://api.test.com/v1",
                model_id="gpt-3.5-turbo",
                messages=[]
            )
        assert constants.ERR_LLM_API_RESPONSE_ERROR in str(excinfo.value)

@pytest.mark.asyncio
async def test_generate_connection_error():
    from aiohttp import ClientConnectorError
    from collections import namedtuple
    MockKey = namedtuple('MockKey', ['host', 'port', 'ssl'])
    mock_key = MockKey(host='invalid.url', port=443, ssl=True)
    conn_error = ClientConnectorError(connection_key=mock_key, os_error=OSError("DNS error"))

    with patch("aiohttp.ClientSession", side_effect=get_mock_session_factory(raise_error=conn_error)):
        with pytest.raises(LLMException) as excinfo:
            await LLMClient.generate(
                api_key="sk-test",
                base_url="https://invalid.url",
                model_id="gpt-3.5-turbo",
                messages=[]
            )
        assert constants.ERR_LLM_CONNECTION_FAILED in str(excinfo.value)
