import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.core.middleware.auditor import audit_command


@pytest.mark.asyncio
async def test_audit_command_success():
    mock_response_content = {
        "choices": [
            {"message": {"content": json.dumps({"score": 2, "reason": "Safe command"})}}
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=mock_response_content)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await audit_command("ls", "http://test.api", "key", "model-1")
        assert result["score"] == 2
        assert result["reason"] == "Safe command"


@pytest.mark.asyncio
async def test_audit_command_http_error():
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await audit_command("ls", "http://test.api", "key", "model-1")
        assert result is None


@pytest.mark.asyncio
async def test_audit_command_invalid_json():
    mock_response_content = {"choices": [{"message": {"content": "Not a JSON string"}}]}
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=mock_response_content)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = await audit_command("ls", "http://test.api", "key", "model-1")
        assert result is None


@pytest.mark.asyncio
async def test_audit_command_exception():
    with patch("aiohttp.ClientSession", side_effect=Exception("Network Down")):
        result = await audit_command("ls", "http://test.api", "key", "model-1")
        assert result is None
