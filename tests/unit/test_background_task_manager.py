import asyncio
from types import SimpleNamespace

import pytest

from app.core.background_tasks import manager as manager_module
from app.core.background_tasks.manager import BackgroundTaskManager
from app.models.message import InternalMessage, InternalToolCall, MessageRole


@pytest.mark.parametrize("next_round_role", [MessageRole.USER, MessageRole.ASSISTANT])
def test_build_submission_context_removes_only_current_sibling_tool_result(next_round_role):
    historical_assistant = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="reused", name="historical_tool", arguments={"value": "history"})],
        created_at=2.0,
    )
    historical_result = InternalMessage(role=MessageRole.TOOL, tool_call_id="reused", content="old-result", created_at=3.0)
    target_call = InternalToolCall(id="target", name="target_tool", arguments={"value": "target"})
    current_assistant = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[target_call, InternalToolCall(id="reused", name="sibling_tool", arguments={"value": "current"})],
        created_at=4.0,
    )
    current_result = InternalMessage(role=MessageRole.TOOL, tool_call_id="reused", content="current-result", created_at=6.0)
    next_round_boundary = InternalMessage(role=next_round_role, content="next-round", created_at=7.0)
    later_assistant = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="reused", name="later_tool", arguments={"value": "later"})],
        created_at=8.0,
    )
    later_result = InternalMessage(role=MessageRole.TOOL, tool_call_id="reused", content="later-result", created_at=9.0)
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="runtime system prompt", created_at=1.0),
        historical_assistant,
        historical_result,
        current_assistant,
        InternalMessage(role=MessageRole.SYSTEM, content="system between turns", created_at=5.0),
        current_result,
        next_round_boundary,
        later_assistant,
        later_result,
    ]
    original_messages = [message.model_dump(mode="json") for message in messages]

    submission_context = manager_module._build_submission_context(messages, "target")

    expected_context = [
        historical_assistant.model_dump(mode="json", exclude_none=True),
        historical_result.model_dump(mode="json", exclude_none=True),
        InternalMessage(role=MessageRole.ASSISTANT, tool_calls=[target_call], created_at=4.0).model_dump(mode="json", exclude_none=True),
        next_round_boundary.model_dump(mode="json", exclude_none=True),
        later_assistant.model_dump(mode="json", exclude_none=True),
        later_result.model_dump(mode="json", exclude_none=True),
    ]
    assert submission_context == expected_context
    assert [message.model_dump(mode="json") for message in messages] == original_messages


def test_build_submission_context_without_matching_target_only_excludes_system_messages():
    historical_assistant = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="reused", name="historical_tool", arguments={"value": "history"})],
        created_at=2.0,
    )
    historical_result = InternalMessage(role=MessageRole.TOOL, tool_call_id="reused", content="old-result", created_at=3.0)
    later_assistant = InternalMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[InternalToolCall(id="reused", name="later_tool", arguments={"value": "later"})],
        created_at=5.0,
    )
    later_result = InternalMessage(role=MessageRole.TOOL, tool_call_id="reused", content="later-result", created_at=6.0)
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="runtime system prompt", created_at=1.0),
        historical_assistant,
        historical_result,
        InternalMessage(role=MessageRole.USER, content="next-round", created_at=4.0),
        later_assistant,
        later_result,
        InternalMessage(role=MessageRole.SYSTEM, content="trailing system prompt", created_at=7.0),
    ]
    original_messages = [message.model_dump(mode="json") for message in messages]

    submission_context = manager_module._build_submission_context(messages, "target")

    expected_context = [
        historical_assistant.model_dump(mode="json", exclude_none=True),
        historical_result.model_dump(mode="json", exclude_none=True),
        messages[3].model_dump(mode="json", exclude_none=True),
        later_assistant.model_dump(mode="json", exclude_none=True),
        later_result.model_dump(mode="json", exclude_none=True),
    ]
    assert submission_context == expected_context
    assert [message.model_dump(mode="json") for message in messages] == original_messages


@pytest.mark.asyncio
async def test_submit_persists_submission_context_with_task_record(monkeypatch):
    manager = BackgroundTaskManager()
    created_task = SimpleNamespace(id=7)
    create_calls = []

    async def create_task(db, **kwargs):
        create_calls.append((db, kwargs))
        return created_task

    monkeypatch.setattr(manager_module.background_task_crud, "create_task", create_task)
    profile = SimpleNamespace(id=3)
    db = object()

    result = await manager.submit(
        db,
        uid="user-1",
        session_id="session-1",
        profile=profile,
        tool_call_id="call-1",
        tool_name="execute_shell",
        arguments={"command": "pwd"},
        messages=[
            InternalMessage(role=MessageRole.SYSTEM, content="runtime system prompt"),
            InternalMessage(role=MessageRole.USER, content="生成两个文件"),
            InternalMessage(
                role=MessageRole.ASSISTANT,
                tool_calls=[
                    InternalToolCall(id="call-1", name="execute_shell", arguments={"command": "pwd"}),
                    InternalToolCall(id="call-2", name="execute_shell", arguments={"command": "ls"}),
                ],
            ),
        ],
    )

    assert result is created_task
    assert len(create_calls) == 1
    extra = create_calls[0][1]["extra"]
    assert extra["source"] == "llm_tool_call"
    assert [message["role"] for message in extra["submission_context"]] == ["user", "assistant"]
    assert extra["submission_context"][0]["content"] == "生成两个文件"
    assert [tool_call["id"] for tool_call in extra["submission_context"][1]["tool_calls"]] == ["call-1"]
    assert manager._task is None
    assert manager._running_by_profile == {}


@pytest.mark.asyncio
async def test_schedule_respects_profile_concurrency_limit(monkeypatch):
    manager = BackgroundTaskManager()
    pending_tasks = [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]
    started_task_ids = []
    release_tasks = asyncio.Event()

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

    async def list_pending(db, *, profile_id, limit):
        assert profile_id == 9
        assert limit == 2
        return pending_tasks[:limit]

    async def run_task(task_id, profile_id):
        started_task_ids.append(task_id)
        await release_tasks.wait()

    monkeypatch.setattr(manager_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(manager_module.background_task_crud, "list_pending", list_pending)
    monkeypatch.setattr(manager, "_run_task", run_task)

    profile = SimpleNamespace(
        id=9,
        configs={"tool": {"background_task_max_concurrency": 2}},
    )
    await manager.schedule(profile)
    await asyncio.sleep(0)

    assert started_task_ids == [1, 2]
    assert len(manager._running_by_profile[9]) == 2

    release_tasks.set()
    await asyncio.gather(*manager._running_by_profile[9])


@pytest.mark.asyncio
async def test_run_loop_periodically_recovers_expired_running_tasks(monkeypatch):
    manager = BackgroundTaskManager()
    recover_calls = 0
    recover_reply_calls = 0
    dispatch_calls = 0
    monotonic_values = iter([0, 30, 30])

    async def recover_tasks():
        nonlocal recover_calls
        recover_calls += 1

    async def recover_replies():
        nonlocal recover_reply_calls
        recover_reply_calls += 1

    async def dispatch_tasks():
        nonlocal dispatch_calls
        dispatch_calls += 1

    async def dispatch_replies():
        manager._stop_event.set()

    monkeypatch.setattr(manager_module, "BACKGROUND_TASK_RECOVERY_INTERVAL_SECONDS", 30)
    monkeypatch.setattr(manager_module, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(manager_module, "recover_pending_background_tasks", recover_tasks)
    monkeypatch.setattr(manager_module, "recover_pending_background_task_replies", recover_replies)
    monkeypatch.setattr(manager, "dispatch_pending_tasks", dispatch_tasks)
    monkeypatch.setattr(manager, "dispatch_pending_replies", dispatch_replies)

    await manager._run_loop()

    assert recover_calls == 1
    assert recover_reply_calls == 1
    assert dispatch_calls == 1


@pytest.mark.asyncio
async def test_dispatch_pending_replies_does_not_wait_for_reply_completion(monkeypatch):
    manager = BackgroundTaskManager()
    reply_started = asyncio.Event()
    release_reply = asyncio.Event()

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc_value, traceback):
            return None

    async def list_pending_replies(db, *, limit):
        assert limit == manager_module.BACKGROUND_TASK_REPLY_MAX_CONCURRENCY
        return [SimpleNamespace(id=11)]

    async def run_reply(task_id):
        assert task_id == 11
        reply_started.set()
        await release_reply.wait()

    monkeypatch.setattr(manager_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(manager_module.background_task_crud, "list_pending_replies", list_pending_replies)
    monkeypatch.setattr(manager, "_run_reply", run_reply)

    await manager.dispatch_pending_replies()
    await reply_started.wait()

    assert len(manager._running_replies) == 1

    release_reply.set()
    await asyncio.gather(*manager._running_replies)


@pytest.mark.asyncio
async def test_stop_cancels_running_background_tasks():
    manager = BackgroundTaskManager()
    started = asyncio.Event()

    async def running_task():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(running_task())
    reply_task = asyncio.create_task(running_task())
    manager._running_by_profile[1].add(task)
    manager._running_replies.add(reply_task)
    await started.wait()
    await asyncio.sleep(0)

    await manager.stop()

    assert task.cancelled()
    assert reply_task.cancelled()
    assert manager._running_by_profile == {}
    assert manager._running_replies == set()
