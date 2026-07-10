import asyncio
from types import SimpleNamespace

import pytest

from app.core.background_tasks import manager as manager_module
from app.core.background_tasks.manager import BackgroundTaskManager


@pytest.mark.asyncio
async def test_submit_only_creates_background_task_record(monkeypatch):
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
    )

    assert result is created_task
    assert len(create_calls) == 1
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
async def test_stop_cancels_running_background_tasks():
    manager = BackgroundTaskManager()
    started = asyncio.Event()

    async def running_task():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(running_task())
    manager._running_by_profile[1].add(task)
    await started.wait()

    await manager.stop()

    assert task.cancelled()
    assert manager._running_by_profile == {}
