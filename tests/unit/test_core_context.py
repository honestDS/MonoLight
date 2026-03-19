import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.context import ContextManager
from app.models.message import Message
from app.models.profile import Profile


@pytest.fixture
def mock_profile():
    profile = MagicMock(spec=Profile)
    profile.context_window_k = 1  # 1KB context window for easy testing
    return profile

def test_estimate_tokens():
    # 测试空字符串
    assert ContextManager.estimate_tokens("") == 0
    # 测试中文 (默认系数 0.6)
    assert ContextManager.estimate_tokens("你好") == 1 # 2 * 0.6 = 1.2 -> 1
    # 测试英文 (默认系数 0.3)
    assert ContextManager.estimate_tokens("abc") == 0 # 3 * 0.3 = 0.9 -> 0
    # 测试混合
    assert ContextManager.estimate_tokens("你好abc") == 2 # 1.2 + 0.9 = 2.1 -> 2

@pytest.mark.asyncio
async def test_get_messages_basic():
    db = MagicMock()
    db.execute = AsyncMock()
    profile = MagicMock(spec=Profile)
    profile.context_window_k = 4
    
    # 模拟历史消息，数据库返回的是倒序：[最新(assistant), 较旧(user)]
    msg1 = Message(role="user", content="hello")
    msg2 = Message(role="assistant", content="hi")
    
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [msg2, msg1]
    db.execute.return_value = mock_result
    
    messages = await ContextManager.get_messages(db, "session1", "uid1", profile, "current")
    
    # 预期：
    # 1. 遍历 msg2 -> temp_messages = [msg2]
    # 2. 遍历 msg1 -> temp_messages = [msg1, msg2]
    # 3. temp_messages[0] 是 user，不执行 pop。
    # 4. append current -> [msg1, msg2, current]
    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[2]["content"] == "current"

@pytest.mark.asyncio
async def test_get_messages_pop_assistant():
    db = MagicMock()
    db.execute = AsyncMock()
    profile = MagicMock(spec=Profile)
    profile.context_window_k = 4
    
    # 模拟只有一条历史消息且是 assistant
    msg1 = Message(role="assistant", content="hi")
    
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [msg1]
    db.execute.return_value = mock_result
    
    messages = await ContextManager.get_messages(db, "session1", "uid1", profile, "current")
    
    # 预期：temp_messages = [msg1]，因为 role 是 assistant，被 pop。
    # 最终只有 current
    assert len(messages) == 1
    assert messages[0]["content"] == "current"

@pytest.mark.asyncio
async def test_get_messages_token_limit():
    db = MagicMock()
    db.execute = AsyncMock()
    profile = MagicMock(spec=Profile)
    profile.context_window_k = 0.1 # Very small window (~80 tokens)
    
    # 模拟一条很长的历史消息
    long_msg = Message(role="user", content="A" * 500)
    
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [long_msg]
    db.execute.return_value = mock_result
    
    messages = await ContextManager.get_messages(db, "session1", "uid1", profile, "current")
    # 历史消息因超出 token 限制而被截断
    assert len(messages) == 1
    assert messages[0]["content"] == "current"

@pytest.mark.asyncio
async def test_get_messages_json_parsing():
    db = MagicMock()
    db.execute = AsyncMock()
    profile = MagicMock(spec=Profile)
    profile.context_window_k = 4
    
    # 模拟一条 JSON 格式的 Tool 调用消息
    tool_content = json.dumps({"role": "assistant", "content": "thinking", "tool_calls": []})
    msg = Message(role="assistant", content=tool_content)
    
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [msg]
    db.execute.return_value = mock_result
    
    messages = await ContextManager.get_messages(db, "session1", "uid1", profile, "current")
    # 解析成功但被 pop
    assert len(messages) == 1
    assert messages[0]["content"] == "current"
