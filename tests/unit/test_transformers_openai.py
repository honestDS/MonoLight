import json

from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.transformers.openai import OpenAITransformer


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
    assert json.loads(provider_msgs[0]["tool_calls"][0]["function"]["arguments"]) == {"a": 1}


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
    assert res.content == "hello"
    assert res.tool_calls[0].name == "shell"
    assert res.tool_calls[0].arguments == {"cmd": "ls"}
