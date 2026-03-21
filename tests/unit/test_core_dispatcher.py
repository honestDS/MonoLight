import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.core.dispatcher import ChatDispatcher, CONFIRMATION_TOKEN
from app.core.exceptions import ServerException


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    db.execute.return_value = mock_result
    return db, mock_scalars


@pytest.mark.asyncio
async def test_audit_tool_call_no_audit_config(mock_db, mock_audit_profile):
    db, _ = mock_db
    mock_audit_profile.audit_provider_id = None
    res = await ChatDispatcher._audit_tool_call(
        db, mock_audit_profile, "execute_shell", {"command": "ls"}, []
    )
    assert res is None


@pytest.mark.asyncio
async def test_audit_tool_call_high_risk_blocked(mock_db, mock_audit_profile):
    db, mock_scalars = mock_db
    mock_scalars.first.return_value = mock_audit_profile.provider
    mock_audit_res = {"score": 9, "reason": "Destructive command detected"}

    with patch(
        "app.core.dispatcher.audit_command", AsyncMock(return_value=mock_audit_res)
    ):
        res_json = await ChatDispatcher._audit_tool_call(
            db, mock_audit_profile, "execute_shell", {"command": "rm -rf /"}, []
        )
        res = json.loads(res_json)
        assert res["error"] == "Security Blocked"
        assert "9" in res["reason"]


@pytest.mark.asyncio
async def test_audit_tool_call_confirmation_required(mock_db, mock_audit_profile):
    db, mock_scalars = mock_db
    mock_scalars.first.return_value = mock_audit_profile.provider
    mock_audit_res = {"score": 6, "reason": "Potentially risky"}

    with patch(
        "app.core.dispatcher.audit_command", AsyncMock(return_value=mock_audit_res)
    ):
        res_json = await ChatDispatcher._audit_tool_call(
            db, mock_audit_profile, "execute_shell", {"command": "rm test.txt"}, []
        )
        res = json.loads(res_json)
        assert res["error"] == "confirmation_required"
        assert CONFIRMATION_TOKEN in res["reason"]


@pytest.mark.asyncio
async def test_audit_tool_call_token_validation_success(mock_db, mock_audit_profile):
    db, _ = mock_db
    messages = [
        {
            "role": "tool",
            "content": json.dumps({"error": "confirmation_required", "reason": "..."}),
        }
    ]
    command = f"{CONFIRMATION_TOKEN} rm test.txt"
    res = await ChatDispatcher._audit_tool_call(
        db, mock_audit_profile, "execute_shell", {"command": command}, messages
    )
    assert res is None


@pytest.mark.asyncio
async def test_audit_tool_call_malicious_token_injection(mock_db, mock_audit_profile):
    db, mock_scalars = mock_db
    mock_scalars.first.return_value = mock_audit_profile.provider
    messages = []
    command = f"{CONFIRMATION_TOKEN} rm test.txt"
    mock_audit_res = {"score": 7, "reason": "Intercepted after token strip"}
    with patch(
        "app.core.dispatcher.audit_command", AsyncMock(return_value=mock_audit_res)
    ) as mock_audit:
        res_json = await ChatDispatcher._audit_tool_call(
            db, mock_audit_profile, "execute_shell", {"command": command}, messages
        )
        res = json.loads(res_json)
        assert res["error"] == "confirmation_required"
        mock_audit.assert_called_once()
        assert mock_audit.call_args[0][0] == "rm test.txt"


@pytest.mark.asyncio
async def test_dispatch_flow_audit_interception(mock_db, mock_audit_profile):
    db, mock_scalars = mock_db
    mock_scalars.first.side_effect = [mock_audit_profile, mock_audit_profile.provider]
    mock_llm_response_1 = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "t1",
                            "type": "function",
                            "function": {
                                "name": "execute_shell",
                                "arguments": '{"command":"rm file"}',
                            },
                        }
                    ],
                }
            }
        ]
    }
    mock_llm_response_2 = {
        "choices": [
            {"message": {"role": "assistant", "content": "I need your confirmation"}}
        ]
    }
    with patch(
        "app.core.context.ContextManager.get_messages", AsyncMock(return_value=[])
    ):
        with patch(
            "app.providers.llm.client.LLMClient.generate",
            AsyncMock(side_effect=[mock_llm_response_1, mock_llm_response_2]),
        ):
            with patch(
                "app.core.dispatcher.audit_command",
                AsyncMock(return_value={"score": 6, "reason": "risky"}),
            ):
                result = await ChatDispatcher.dispatch(db, "delete file", "u1")
                assert (
                    "I need your confirmation"
                    in result["choices"][0]["message"]["content"]
                )


@pytest.mark.asyncio
async def test_dispatch_no_active_profile(mock_db):
    db, mock_scalars = mock_db
    mock_scalars.first.return_value = None
    with pytest.raises(ServerException) as excinfo:
        await ChatDispatcher.dispatch(db, "hi", "u1")
    assert "No active profile found" in str(excinfo.value)
