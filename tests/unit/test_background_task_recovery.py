from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.background_tasks import recovery as recovery_module
from app.core.background_tasks.recovery import recover_pending_background_tasks


class FakeSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_recover_pending_background_tasks_triggers_reply_for_failed_retries(monkeypatch):
    fake_db = SimpleNamespace()
    fake_profile = SimpleNamespace(id=1)
    fake_background_task_manager = SimpleNamespace(schedule=AsyncMock())
    fake_trigger = AsyncMock()
    fake_crud = SimpleNamespace(requeue_expired_running=AsyncMock(return_value=[9]))
    fake_profile_crud = SimpleNamespace(get_multi=AsyncMock(return_value=[fake_profile]))

    monkeypatch.setattr(recovery_module, "AsyncSessionLocal", lambda: FakeSessionContext(fake_db))
    monkeypatch.setattr(recovery_module, "background_task_crud", fake_crud)
    monkeypatch.setattr(recovery_module, "profile_crud", fake_profile_crud)
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.core.background_tasks.manager",
        SimpleNamespace(background_task_manager=fake_background_task_manager),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.core.background_tasks.reply_trigger",
        SimpleNamespace(trigger_background_task_reply=fake_trigger),
    )

    await recover_pending_background_tasks()

    fake_crud.requeue_expired_running.assert_awaited_once_with(fake_db, profile_id=1)
    fake_background_task_manager.schedule.assert_awaited_once_with(fake_profile)
    fake_trigger.assert_awaited_once_with(9)
