from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.background_tasks import scheduler as scheduler_module
from app.core.background_tasks.scheduler import ScheduledTaskScheduler
from app.core.utils.time import get_local_time
from app.models.profile import ProfileConfig
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.models.system_setting import SystemRuntimeSettings


def test_task_concurrency_defaults_are_applied_to_existing_profile_configs():
    cfg = ProfileConfig.model_validate({"tool": {}})

    assert cfg.tool.background_task_max_concurrency == 2
    assert cfg.tool.scheduled_task_max_concurrency == 4


def test_session_reply_global_concurrency_default():
    settings = SystemRuntimeSettings()

    assert settings.session_reply_max_concurrency == 4


@pytest.mark.parametrize("value", [0, 101])
def test_session_reply_global_concurrency_range(value):
    with pytest.raises(ValidationError):
        SystemRuntimeSettings(session_reply_max_concurrency=value)


@pytest.mark.asyncio
async def test_scheduled_task_claim_disables_session_synchronization(monkeypatch):
    scheduler = ScheduledTaskScheduler()
    scheduled_task = ScheduledTask(
        id=7,
        uid="user-1",
        session_id="session-1",
        name="test",
        message="run",
        profile_id=3,
        status=ScheduledTaskStatus.ENABLED,
        interval_seconds=60,
        next_run_at=get_local_time(),
    )
    statements = []

    class FakeDb:
        def add(self, instance) -> None:
            instance.id = 11

        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(rowcount=1)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    async def get_session(db, session_id):
        return SimpleNamespace(uid="user-1")

    async def get_profile(db, profile_id):
        return SimpleNamespace(id=3, uid="user-1")

    async def get_user(db, uid):
        return SimpleNamespace(username="user")

    async def enqueue_summary(db, **kwargs):
        return None

    monkeypatch.setattr(scheduler_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(scheduler_module.profile_crud, "get_with_relations", get_profile)
    monkeypatch.setattr(scheduler_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(scheduler_module.session_reply_queue_manager, "enqueue_scheduled_summary", enqueue_summary)

    await scheduler._dispatch_one_with_db(FakeDb(), scheduled_task)

    assert len(statements) == 2
    assert statements[0].get_execution_options()["synchronize_session"] is False
    assert statements[1].get_execution_options()["synchronize_session"] is False
