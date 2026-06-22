import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.dispatcher import ChatDispatcher, _resolve_chat_params
from app.core.exceptions import LLMException
from app.core.utils.dispatcher.audit_tool_call import audit_tool_call


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

    with pytest.raises(LLMException) as exc:
        await ChatDispatcher.dispatch(db, "hi", "u1")
    assert exc.value.message == "ERR_PROFILE_NOT_FOUND"


@pytest.mark.asyncio
async def test_audit_tool_call_blocked(mock_profile):
    db = MagicMock()
    db.execute = AsyncMock()
    # 模拟 audit_provider 查询
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_profile.provider
    db.execute.return_value = mock_result

    with patch("app.core.middleware.auditor.ShellExecutor.check_blacklist", return_value="rm -rf"):
        res_json = await audit_tool_call(db, mock_profile, MagicMock(), "execute_shell", {"command": "rm -rf /"}, [])
    res = json.loads(res_json)
    assert res["error"] == "Security Blocked"


def test_resolve_chat_params_uses_profile_chat_timeout():
    chat_channel = MagicMock(chat_timeout=180)
    model_entry = {
        "model_id": "primary-model",
        "context_window_k": 8,
        "max_tokens": 1024,
    }

    params = _resolve_chat_params(model_entry, chat_channel)

    assert params["chat_timeout"] == 180
    assert params["context_window_k"] == 8
    assert params["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_validate_initial_message_before_save_only_checks_primary_channel(mock_profile):
    db = MagicMock()
    db.execute = AsyncMock()

    cfg = MagicMock()
    cfg.channel.chat_channel = MagicMock(retry_on_failure=True, chat_timeout=60)

    channel = MagicMock()
    model_entry = {
        "model_id": "primary-model",
        "context_window_k": 8,
        "max_tokens": 1024,
    }
    channel_rule = MagicMock(priority=1)

    with patch("app.core.dispatcher.validate_profile_and_cfg", AsyncMock(return_value=cfg)), patch(
        "app.core.dispatcher.select_channel",
        AsyncMock(return_value=(channel, model_entry, channel_rule)),
    ) as mock_select_channel, patch(
        "app.core.dispatcher.is_embedding_profile_available",
        AsyncMock(return_value=False),
    ), patch(
        "app.core.dispatcher.build_system_prompt",
        AsyncMock(return_value="system prompt"),
    ), patch(
        "app.core.dispatcher.get_tools_for_profile",
        AsyncMock(return_value=([], [])),
    ), patch(
        "app.core.dispatcher.append_session_markdown_instruction",
        AsyncMock(),
    ):
        await ChatDispatcher.validate_initial_message_before_save(
            db=db,
            message="hello",
            uid="u1",
            session_id="s1",
            profile=mock_profile,
        )

    assert mock_select_channel.call_count == 1
    assert mock_select_channel.await_args.kwargs["cursor_key"] is None
    assert mock_select_channel.await_args.kwargs["log_selection"] is False
    assert "excluded_priorities" not in mock_select_channel.await_args.kwargs
