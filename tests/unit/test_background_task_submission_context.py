import json
from types import SimpleNamespace

import pytest

from app.core.session_reply_queue import executor as executor_module
from app.models.message import InternalMessage, InternalToolCall, MessageRole


@pytest.mark.asyncio
async def test_background_summary_uses_submission_context_and_task_result(monkeypatch):
    task = SimpleNamespace(
        id=8,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        tool_call_id="call-1",
        tool_name="execute_shell",
        status="succeeded",
        result={"status": "succeeded", "output": "snapshot result"},
        error=None,
        extra={
            "submission_context": [
                InternalMessage(role=MessageRole.USER, content="提交任务前的消息").model_dump(mode="json", exclude_none=True),
                InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="call-1",
                            name="execute_shell",
                            arguments={"command": "pwd"},
                        )
                    ],
                ).model_dump(mode="json", exclude_none=True),
            ]
        },
    )
    work = SimpleNamespace(
        id=12,
        source_id="8",
        uid="user-1",
        session_id="session-1",
        profile_id=3,
    )
    profile = SimpleNamespace(id=3, uid="user-1")
    captured = {}

    async def get_task(_db, task_id):
        assert task_id == 8
        return task

    async def get_profile(_db, profile_id):
        assert profile_id == 3
        return profile

    async def generate_reply(_db, **kwargs):
        captured.update(kwargs)
        return InternalMessage(role=MessageRole.ASSISTANT, content="后台总结"), [], []

    monkeypatch.setattr(executor_module.background_task_crud, "get", get_task)
    monkeypatch.setattr(executor_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(executor_module.ChatDispatcher, "_generate_reply_from_history", generate_reply)

    response = await executor_module._execute_background(object(), work)

    assert response["content"] == "后台总结"
    assert [message.content for message in captured["submission_context"] if message.role == MessageRole.USER] == ["提交任务前的消息"]
    assert captured["submission_context"][-1].tool_calls[0].id == "call-1"
    assert captured["extra_messages"][0].role == MessageRole.TOOL
    assert captured["extra_messages"][0].tool_call_id == "call-1"
    assert json.loads(captured["extra_messages"][0].content)["output"] == "snapshot result"
    assert captured["extra_messages"][1].role == MessageRole.USER

