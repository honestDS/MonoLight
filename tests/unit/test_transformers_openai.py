import json
from app.transformers.openai import OpenAITransformer
from app.schemas.message import InternalMessage, MessageRole, InternalToolCall


def test_openai_to_provider():
    msg = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="thinking",
        tool_calls=[InternalToolCall(id="1", name="test", arguments={"a": 1})],
    )
    provider_msgs = OpenAITransformer.to_provider([msg])
    assert len(provider_msgs) == 1
    assert provider_msgs[0]["role"] == "assistant"
    assert "tool_calls" in provider_msgs[0]
    assert json.loads(provider_msgs[0]["tool_calls"][0]["function"]["arguments"]) == {
        "a": 1
    }


def test_openai_from_provider():
    resp_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "hello",
                    "tool_calls": [
                        {
                            "id": "t1",
                            "type": "function",
                            "function": {"name": "shell", "arguments": '{"cmd": "ls"}'},
                        }
                    ],
                }
            }
        ],
        "model": "gpt-4",
        "usage": {"total_tokens": 100},
    }
    res = OpenAITransformer.from_provider(resp_data)
    assert res.message.content == "hello"
    assert res.message.tool_calls[0].name == "shell"
    assert res.usage["total_tokens"] == 100
