from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.background_tasks import runner as runner_module
from app.core.background_tasks.runner import run_background_task
from app.models.background_task import BackgroundTaskStatus


class FakeSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _patch_session_factory(monkeypatch, db):
    monkeypatch.setattr(runner_module, "AsyncSessionLocal", lambda: FakeSessionContext(db))


def _build_task(**kwargs):
    data = {
        "id": 7,
        "uid": "user_1",
        "session_id": "session_1",
        "profile_id": 1,
        "tool_name": "fake_tool",
        "arguments": {"value": "ok", "run_in_background": True},
        "extra": {},
        "status": BackgroundTaskStatus.RUNNING,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_run_background_task_does_not_mark_success_when_cancelled_after_execute(monkeypatch):
    task = _build_task()

    class FakeDb:
        async def refresh(self, db_task):
            db_task.status = BackgroundTaskStatus.CANCELLED

    class FakeExecutor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def set_config(self, cfg):
            self.cfg = cfg

        async def execute(self, **kwargs):
            assert kwargs == {"value": "ok"}
            return "done"

    fake_crud = SimpleNamespace(
        try_claim=AsyncMock(return_value=task),
        get=AsyncMock(return_value=task),
        mark_succeeded=AsyncMock(side_effect=AssertionError("cancelled task should not be marked succeeded")),
        mark_failed=AsyncMock(side_effect=AssertionError("cancelled task should not be marked failed")),
    )
    monkeypatch.setattr(runner_module, "background_task_crud", fake_crud)
    monkeypatch.setattr(runner_module.profile_crud, "get", AsyncMock(return_value=SimpleNamespace(id=1, configs={})))
    monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, "fake_tool", FakeExecutor)
    monkeypatch.setattr(runner_module, "trigger_background_task_reply", AsyncMock(side_effect=AssertionError("cancelled task should not trigger reply")))
    _patch_session_factory(monkeypatch, FakeDb())

    await run_background_task(7, worker_id="worker_1")

    fake_crud.mark_succeeded.assert_not_awaited()
    fake_crud.mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_background_task_does_not_mark_failure_when_cancelled_after_error(monkeypatch):
    task = _build_task()

    class FakeDb:
        async def refresh(self, db_task):
            db_task.status = BackgroundTaskStatus.CANCELLED

    class FakeExecutor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def execute(self, **kwargs):
            raise RuntimeError("boom")

    fake_crud = SimpleNamespace(
        try_claim=AsyncMock(return_value=task),
        get=AsyncMock(return_value=task),
        mark_succeeded=AsyncMock(side_effect=AssertionError("cancelled task should not be marked succeeded")),
        mark_failed=AsyncMock(side_effect=AssertionError("cancelled task should not be marked failed")),
    )
    monkeypatch.setattr(runner_module, "background_task_crud", fake_crud)
    monkeypatch.setattr(runner_module.profile_crud, "get", AsyncMock(return_value=SimpleNamespace(id=1, configs={})))
    monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, "fake_tool", FakeExecutor)
    monkeypatch.setattr(runner_module, "trigger_background_task_reply", AsyncMock(side_effect=AssertionError("cancelled task should not trigger reply")))
    _patch_session_factory(monkeypatch, FakeDb())

    await run_background_task(7, worker_id="worker_1")

    fake_crud.mark_succeeded.assert_not_awaited()
    fake_crud.mark_failed.assert_not_awaited()
