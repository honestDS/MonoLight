import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import ServerException


@pytest.fixture
def mock_profile():
    p = MagicMock()
    p.id = 1
    p.configs = {
        "provider": {"model_id": "gpt-4", "temperature": 0.7},
        "security": {
            "audit_provider_id": 1,
            "audit_model_id": "audit",
            "audit_threshold": 5,
        },
        "tool": {"shell_timeout": 30.0},
        "other": {"context_window_k": 4},
    }
    p.provider = MagicMock()
    p.provider.api_key = "key"
    p.provider.base_url = "url"
    p.prompt = None
    return p


@pytest.mark.asyncio
async def test_dispatch_no_active_profile():
    db = MagicMock()
    db.execute = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    db.execute.return_value = mock_result

    with pytest.raises(ServerException) as exc:
        await ChatDispatcher.dispatch(db, "hi", "u1")
    assert "No active profile found" in str(exc.value)


@pytest.mark.asyncio
async def test_audit_tool_call_blocked(mock_profile):
    db = MagicMock()
    db.execute = AsyncMock()
    # 模拟 audit_provider 查询
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_profile.provider
    db.execute.return_value = mock_result

    with patch(
        "app.core.dispatcher.audit_command",
        AsyncMock(return_value={"score": 9, "reason": "danger"}),
    ):
        res_json = await ChatDispatcher._audit_tool_call(
            db, mock_profile, "execute_shell", {"command": "rm -rf /"}, []
        )
        res = json.loads(res_json)
        assert res["error"] == "Security Blocked"
