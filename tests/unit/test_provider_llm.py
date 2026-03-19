import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from app.core import constants
from app.core.exceptions import LLMException
from app.providers.llm.client import LLMClient


@pytest.mark.asyncio
async def test_generate_success():
    mock_response_data = {"choices": [{"message": {"role": "assistant", "content": "hello world"}}]}
    mock_response_json = json.dumps(mock_response_data)
    
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(side_effect=lambda: mock_response_json)
    
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_resp
    
    with patch("aiohttp.ClientSession.post", return_value=mock_ctx):
        result = await LLMClient.generate(
            api_key="sk-test",
            base_url="https://api.test.com/v1",
            model_id="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "hi"}]
        )
        assert result["choices"][0]["message"]["content"] == "hello world"

@pytest.mark.asyncio
async def test_generate_http_error():
    mock_resp = MagicMock()
    mock_resp.status = 401
    mock_resp.text = AsyncMock(side_effect=lambda: "Unauthorized")
    
    mock_ctx = MagicMock()
    mock_ctx.__aenter__.return_value = mock_resp
    
    with patch("aiohttp.ClientSession.post", return_value=mock_ctx):
        with pytest.raises(LLMException) as excinfo:
            await LLMClient.generate(
                api_key="invalid",
                base_url="https://api.test.com/v1",
                model_id="gpt-3.5-turbo",
                messages=[]
            )
        assert constants.ERR_LLM_API_RESPONSE_ERROR in str(excinfo.value)
        assert "401" in str(excinfo.value)

@pytest.mark.asyncio
async def test_generate_connection_error():
    with patch("aiohttp.ClientSession.post", side_effect=aiohttp.ClientConnectorError(
        connection_key=MagicMock(),
        os_error=OSError("DNS error")
    )):
        with pytest.raises(LLMException) as excinfo:
            await LLMClient.generate(
                api_key="sk-test",
                base_url="https://invalid.url",
                model_id="gpt-3.5-turbo",
                messages=[]
            )
        assert constants.ERR_LLM_CONNECTION_FAILED in str(excinfo.value)
