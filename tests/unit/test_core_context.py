from unittest.mock import AsyncMock, MagicMock

import pytest
from app.schemas.message import MessageRole

from app.core.context import ContextManager
from app.models.message import Message
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
    assert ContextManager.estimate_tokens("你好") == 1
    assert ContextManager.estimate_tokens("abc") == 0


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

    assert len(messages) == 3
    assert messages[0].role == MessageRole.USER
    assert messages[1].role == MessageRole.ASSISTANT
    assert messages[2].content == "current"


@pytest.mark.asyncio
async def test_get_messages_token_limit(mock_profile):
    db = MagicMock()
    db.execute = AsyncMock()
    # 修复：context_window_k 必须是整数
    mock_profile.configs["other"]["context_window_k"] = 1

    # 模拟一条非常巨大的历史消息，使其超出 token 限制
    # limit_tokens = 1 * 1024 * 0.8 = 819.2 tokens
    long_msg = Message(role="user", content="A" * 4000)
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [long_msg]
    db.execute.return_value = mock_result

    messages = await ContextManager.get_messages(db, "s1", "u1", mock_profile, "current")
    assert len(messages) == 1
    assert messages[0].content == "current"
