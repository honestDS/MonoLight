import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from app.core.dispatcher import ChatDispatcher
from app.models.profile import Profile
from app.models.provider import ModelProvider
from app.models.prompt import PromptLibrary
from app.core import constants

@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    mock_result = MagicMock()
    mock_scalars = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    db.execute.return_value = mock_result
    return db, mock_scalars

def create_mock_profile():
    mock_profile = MagicMock(spec=Profile)
    mock_profile.id = 1
    mock_profile.is_active = True
    mock_profile.model_id = "gpt-4"
    mock_profile.temperature = 0.7
    mock_profile.max_tokens = 1000
    mock_profile.provider = MagicMock(spec=ModelProvider)
    mock_profile.provider.api_key = "sk-test"
    mock_profile.provider.base_url = "http://test.api"
    mock_profile.prompt = None
    return mock_profile

@pytest.mark.asyncio
async def test_dispatch_no_active_profile(mock_db):
    db, mock_scalars = mock_db
    mock_scalars.first.return_value = None
    result = await ChatDispatcher.dispatch(db, "hi", "u1")
    assert "No active profile found" in result["choices"][0]["message"]["content"]

@pytest.mark.asyncio
async def test_dispatch_no_provider(mock_db):
    db, mock_scalars = mock_db
    mock_profile = create_mock_profile()
    mock_profile.provider = None 
    mock_scalars.first.return_value = mock_profile
    result = await ChatDispatcher.dispatch(db, "hi", "u1")
    assert result["error"] is True
    assert constants.ERR_PROFILE_PROVIDER_MISMATCH in result["choices"][0]["message"]["content"]

@pytest.mark.asyncio
async def test_dispatch_with_system_prompt_injection(mock_db):
    db, mock_scalars = mock_db
    mock_profile = create_mock_profile()
    mock_profile.prompt = MagicMock(spec=PromptLibrary)
    mock_profile.prompt.content = "You are a helper"
    mock_scalars.first.return_value = mock_profile
    
    existing_msgs = [{"role": "system", "content": "Old system"}, {"role": "user", "content": "hi"}]
    with patch("app.core.context.ContextManager.get_messages", AsyncMock(return_value=existing_msgs)):
        with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(return_value={"choices":[{"message":{"role":"assistant","content":"ok"}}]})) as mock_gen:
            await ChatDispatcher.dispatch(db, "hi", "u1")
            _, kwargs = mock_gen.call_args
            sent_messages = kwargs['messages']
            assert sent_messages[0]['content'] == "You are a helper"
            assert len([m for m in sent_messages if m['role'] == 'system']) == 1

@pytest.mark.asyncio
async def test_dispatch_tool_call_loop_execution(mock_db):
    db, mock_scalars = mock_db
    mock_profile = create_mock_profile()
    mock_scalars.first.return_value = mock_profile

    mock_llm_responses = [
        {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "execute_shell", "arguments": '{"command": "ls"}'}
                    }]
                }
            }]
        },
        {
            "choices": [{"message": {"role": "assistant", "content": "File list shown"}}]
        }
    ]

    with patch("app.core.context.ContextManager.get_messages", AsyncMock(return_value=[])):
        with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(side_effect=mock_llm_responses)):
            with patch("app.core.tools.shell.ShellExecutor.execute", AsyncMock(return_value="file1.txt")):
                result = await ChatDispatcher.dispatch(db, "ls please", "u1")
                assert result["choices"][0]["message"]["content"] == "File list shown"

@pytest.mark.asyncio
async def test_dispatch_tool_argument_error(mock_db):
    db, mock_scalars = mock_db
    mock_profile = create_mock_profile()
    mock_scalars.first.return_value = mock_profile

    mock_llm_response = {
        "choices": [{
            "message": {
                "role": "assistant",
                "tool_calls": [{
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "execute_shell", "arguments": "INVALID_JSON"}
                }]
            }
        }]
    }
    mock_llm_final = {"choices": [{"message": {"role": "assistant", "content": "error handled"}}]}

    with patch("app.core.context.ContextManager.get_messages", AsyncMock(return_value=[])):
        with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(side_effect=[mock_llm_response, mock_llm_final])):
            result = await ChatDispatcher.dispatch(db, "bad tool", "u1")
            assert result["choices"][0]["message"]["content"] == "error handled"

@pytest.mark.asyncio
async def test_dispatch_llm_api_key_none_error(mock_db):
    db, mock_scalars = mock_db
    mock_profile = create_mock_profile()
    # 模拟 provider.api_key 访问报错触发 try-except 捕获逻辑
    type(mock_profile.provider).api_key = PropertyMock(side_effect=Exception("'NoneType' object has no attribute 'api_key'"))
    mock_scalars.first.return_value = mock_profile
    
    with patch("app.core.context.ContextManager.get_messages", AsyncMock(return_value=[])):
        result = await ChatDispatcher.dispatch(db, "hi", "u1")
        assert constants.ERR_LLM_PROVIDER_NOT_CONFIGURED in result["choices"][0]["message"]["content"]

@pytest.mark.asyncio
async def test_dispatch_max_turns_limit(mock_db):
    db, mock_scalars = mock_db
    mock_profile = create_mock_profile()
    mock_scalars.first.return_value = mock_profile
    
    infinite_tool_call = {
        "choices": [{
            "message": {
                "role": "assistant",
                "tool_calls": [{"id": "tx", "type": "function", "function": {"name": "execute_shell", "arguments": '{"command":"ls"}'}}]
            }
        }]
    }
    
    with patch("app.core.context.ContextManager.get_messages", AsyncMock(return_value=[])):
        with patch("app.providers.llm.client.LLMClient.generate", AsyncMock(return_value=infinite_tool_call)):
            with patch("app.core.tools.shell.ShellExecutor.execute", AsyncMock(return_value="done")):
                result = await ChatDispatcher.dispatch(db, "loop", "u1")
                assert result["choices"][0]["message"]["content"] == ""
