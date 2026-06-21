import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.context import ContextManager
from app.core.utils.dispatcher.truncate_tool_result import truncate_tool_result, truncate_tool_result_with_stats
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole
from app.models.profile import Profile


@pytest.fixture
def mock_profile():
    profile = MagicMock(spec=Profile)
    # 必须提供 configs 字典
    profile.configs = {
        "provider": {"model_id": "test", "temperature": 0.7},
        "security": {"audit_threshold": 5},
        "tool": {"shell_timeout": 30.0},
        "other": {"context_window_k": 1},
    }
    return profile


def test_estimate_tokens():
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("abc") == 1


@pytest.mark.asyncio
async def test_get_messages_basic(mock_profile):
    db = MagicMock()
    db.execute = AsyncMock()

    msg1 = Message(role="user", content="hello")
    msg2 = Message(role="assistant", content="hi")

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [msg2, msg1]
    db.execute.return_value = mock_result

    messages = await ContextManager.get_messages(db, "s1", "u1", mock_profile, "current")

    assert len(messages) == 2
    assert messages[0].role == MessageRole.USER
    assert messages[1].role == MessageRole.ASSISTANT


@pytest.mark.asyncio
async def test_get_messages_token_limit(mock_profile):
    db = MagicMock()
    db.execute = AsyncMock()
    # 修复：context_window_k 必须是整数
    mock_profile.configs["other"]["context_window_k"] = 1

    # 模拟一条非常巨大的历史消息。当前策略会在历史窗口不足时丢弃更早消息，
    # 但当仅有当前这条历史消息时会保留该消息。
    long_msg = Message(role="user", content="A" * 4000)
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [long_msg]
    db.execute.return_value = mock_result

    messages = await ContextManager.get_messages(db, "s1", "u1", mock_profile, "current")
    assert len(messages) == 1
    assert messages[0].content == "A" * 4000


def test_truncate_tool_result_with_stats_keeps_tuple_wrapper_compatible():
    content = "工具响应内容" * 2000
    result = truncate_tool_result_with_stats(content, context_window_k=1)
    tuple_content, tuple_truncated = truncate_tool_result(content, context_window_k=1)

    assert result.truncated is True
    assert result.content == tuple_content
    assert result.truncated == tuple_truncated
    assert result.original_tokens > result.final_tokens
    assert result.removed_chars > 0


def test_context_tool_result_uses_shared_token_truncation():
    content = "工具响应内容" * 2000
    expected_content, expected_truncated = truncate_tool_result(content, context_window_k=1)

    parsed_history = [
        InternalMessage(role=MessageRole.TOOL, tool_call_id="call_1", content=content),
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[InternalToolCall(id="call_1", name="demo_tool", arguments={})],
        ),
        InternalMessage(role=MessageRole.USER, content="run tool"),
    ]
    expected_before = sum(
        estimate_tokens(json.dumps(msg.model_dump()) if msg.tool_calls else (msg.content or ""))
        for msg in parsed_history
    )

    messages, log_data = ContextManager._strategy_atomic_truncate(
        uid="u1",
        session_id="s1",
        parsed_history=[msg.model_copy(deep=True) for msg in parsed_history],
        limit_tokens=100000,
        current_msg_tokens=0,
        context_window_k=1,
    )
    expected_after = sum(
        estimate_tokens(json.dumps(msg.model_dump()) if msg.tool_calls else (msg.content or ""))
        for msg in messages
    )

    assert expected_truncated is True
    assert messages[2].role == MessageRole.TOOL
    assert messages[2].content == expected_content
    assert log_data["before"] == expected_before
    assert log_data["after"] == expected_after
    assert log_data["before"] > log_data["after"]
