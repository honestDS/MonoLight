import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.dispatcher import ChatDispatcher, _resolve_chat_params
from app.core.exceptions import LLMException
from app.core.prompts import SYSTEM_RUNTIME_CONTEXT_POLICY
from app.core.utils.dispatcher.audit_tool_call import audit_tool_call
from app.core.utils.dispatcher.inject_system_prompt import build_system_prompt
from app.core.utils.dispatcher.prepare_messages import prepare_messages
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole, TextPart


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
        "tool": {"tool_timeout": 30.0},
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


@pytest.mark.asyncio
async def test_build_system_prompt_includes_runtime_context_policy_without_profile_prompt(mock_profile):
    mock_profile.prompt = None

    with patch(
        "app.core.utils.dispatcher.inject_system_prompt.list_available_knowledge_bases",
        AsyncMock(return_value=[]),
    ):
        system_prompt = await build_system_prompt(MagicMock(), mock_profile, embedding_profile_available=True)

    assert SYSTEM_RUNTIME_CONTEXT_POLICY in system_prompt


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
async def test_prepare_messages_does_not_mutate_initial_message_on_first_iter_retry(mock_profile):
    db = MagicMock()
    cfg = MagicMock()
    initial_msg = InternalMessage(id=10, role=MessageRole.USER, content="hello")

    with (
        patch(
            "app.core.utils.dispatcher.prepare_messages.build_system_prompt",
            AsyncMock(return_value="system prompt"),
        ),
        patch(
            "app.core.utils.dispatcher.prepare_messages.build_user_runtime_instructions",
            AsyncMock(return_value="\n\nruntime instructions"),
        ),
        patch(
            "app.core.utils.dispatcher.prepare_messages.ContextManager.get_messages",
            AsyncMock(return_value=[]),
        ),
        patch(
            "app.core.utils.dispatcher.prepare_messages.inject_system_prompt_text",
            side_effect=lambda messages, system_prompt: messages,
        ),
    ):
        first_messages = await prepare_messages(
            db=db,
            session_id="s1",
            uid="u1",
            profile=mock_profile,
            cfg=cfg,
            initial_msg=initial_msg,
            message="hello",
            is_first_iter=True,
        )
        retry_messages = await prepare_messages(
            db=db,
            session_id="s1",
            uid="u1",
            profile=mock_profile,
            cfg=cfg,
            initial_msg=initial_msg,
            message="hello",
            is_first_iter=True,
        )

    assert initial_msg.content == "hello"
    assert first_messages[-1] is not initial_msg
    assert retry_messages[-1] is not initial_msg
    assert first_messages[-1].content == "hello\n\nruntime instructions"
    assert retry_messages[-1].content == "hello\n\nruntime instructions"
    assert retry_messages[-1].content.count("runtime instructions") == 1


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

    captured_budget_kwargs = {}

    def capture_budget_call(**kwargs):
        captured_budget_kwargs.update(kwargs)

    with (
        patch("app.core.dispatcher.validate_profile_and_cfg", AsyncMock(return_value=cfg)),
        patch(
            "app.core.dispatcher.select_channel",
            AsyncMock(return_value=(channel, model_entry, channel_rule)),
        ) as mock_select_channel,
        patch(
            "app.core.dispatcher.is_embedding_profile_available",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.core.dispatcher.build_system_prompt",
            AsyncMock(return_value="system prompt"),
        ),
        patch(
            "app.core.dispatcher.get_tools_for_profile",
            AsyncMock(return_value=([], [])),
        ),
        patch(
            "app.core.dispatcher.build_user_runtime_instructions",
            AsyncMock(return_value="runtime instructions"),
        ),
        patch(
            "app.core.dispatcher.ContextManager.validate_latest_user_message_budget",
            side_effect=capture_budget_call,
        ),
    ):
        await ChatDispatcher.validate_initial_message_before_save(
            db=db,
            message=[{"type": "text", "text": "hello"}],
            uid="u1",
            session_id="s1",
            profile=mock_profile,
        )

    assert mock_select_channel.call_count == 1
    assert mock_select_channel.await_args.kwargs["cursor_key"] is None
    assert mock_select_channel.await_args.kwargs["log_selection"] is False
    assert "excluded_priorities" not in mock_select_channel.await_args.kwargs
    assert captured_budget_kwargs["message"].content == [TextPart(text="hello")]
    assert captured_budget_kwargs["system_tokens"] == estimate_tokens("system prompt") + estimate_tokens("runtime instructions")


@pytest.mark.asyncio
async def test_validate_initial_message_before_save_does_not_mutate_validation_multimodal_content(mock_profile):
    db = MagicMock()
    db.execute = AsyncMock()

    cfg = MagicMock()
    cfg.channel.chat_channel = MagicMock(retry_on_failure=True, chat_timeout=60)

    channel = MagicMock()
    model_entry = {
        "model_id": "primary-model",
        "context_window_k": 8,
        "max_tokens": 1024,
        "image_understanding": True,
    }
    channel_rule = MagicMock(priority=1)
    message = [{"type": "text", "text": "hello"}]

    captured_messages = []

    def capture_budget_message(message, *args, **kwargs):
        captured_messages.append(message)

    with (
        patch("app.core.dispatcher.validate_profile_and_cfg", AsyncMock(return_value=cfg)),
        patch(
            "app.core.dispatcher.select_channel",
            AsyncMock(return_value=(channel, model_entry, channel_rule)),
        ),
        patch(
            "app.core.dispatcher.is_embedding_profile_available",
            AsyncMock(return_value=False),
        ),
        patch(
            "app.core.dispatcher.build_system_prompt",
            AsyncMock(return_value="system prompt"),
        ),
        patch(
            "app.core.dispatcher.get_tools_for_profile",
            AsyncMock(return_value=([], [])),
        ),
        patch(
            "app.core.dispatcher.build_user_runtime_instructions",
            AsyncMock(return_value="\n\nruntime instructions"),
        ),
        patch(
            "app.core.dispatcher.ContextManager.validate_latest_user_message_budget",
            side_effect=capture_budget_message,
        ),
    ):
        await ChatDispatcher.validate_initial_message_before_save(
            db=db,
            message=message,
            uid="u1",
            session_id="s1",
            profile=mock_profile,
        )

    assert message == [{"type": "text", "text": "hello"}]
    assert captured_messages
    validation_msg = captured_messages[0]
    assert validation_msg.role == MessageRole.USER
    assert validation_msg.content == [TextPart(text="hello")]
