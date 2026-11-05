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
    assert cfg.tool.subagent_max_concurrency == 4


def test_agent_work_global_concurrency_default():
    settings = SystemRuntimeSettings()

    assert settings.agent_work_max_concurrency == 4


@pytest.mark.parametrize("value", [0, 101])
def test_agent_work_global_concurrency_range(value):
    with pytest.raises(ValidationError):
        SystemRuntimeSettings(agent_work_max_concurrency=value)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"tool": {"subagent_max_concurrency": 1}}, 1),
        ({"tool": {"subagent_max_concurrency": 20}}, 20),
        ({"subagent_max_concurrency": 1}, 1),
        ({"subagent_max_concurrency": 20}, 20),
    ],
)
def test_subagent_concurrency_range_accepts_nested_and_flat_configs(config, expected):
    cfg = ProfileConfig.model_validate(config)

    assert cfg.tool.subagent_max_concurrency == expected


@pytest.mark.parametrize(
    "config",
    [
        {"tool": {"subagent_max_concurrency": 0}},
        {"tool": {"subagent_max_concurrency": 21}},
        {"subagent_max_concurrency": 0},
        {"subagent_max_concurrency": 21},
    ],
)
def test_subagent_concurrency_range_rejects_nested_and_flat_configs(config):
    with pytest.raises(ValidationError):
        ProfileConfig.model_validate(config)


@pytest.mark.asyncio
async def test_scheduled_task_claim_disables_session_synchronization(monkeypatch):
    scheduler = ScheduledTaskScheduler()
    scheduled_task = ScheduledTask(
        id=7,
        uid="user-1",
        session_id="session-1",
        name="test",
        message="run",
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

    async def resolve_profile(db, *, uid, session_id):
        assert uid == "user-1"
        assert session_id == "session-1"
        return SimpleNamespace(id=3, uid="user-1")

    async def lock_profile(db, *, profile_id, uid):
        assert profile_id == 3
        assert uid == "user-1"
        return SimpleNamespace(id=3, uid="user-1")

    async def get_user(db, uid):
        return SimpleNamespace(username="user")

    async def enqueue_summary(db, **kwargs):
        return SimpleNamespace(id=13), True

    async def persist_message(db, *, message, commit=True, update_session_activity=True):
        db.add(message)
        await db.flush()
        return message

    monkeypatch.setattr(scheduler_module.session_crud, "get_by_session_id", get_session)
    monkeypatch.setattr(scheduler_module, "resolve_profile_for_session", resolve_profile)
    monkeypatch.setattr(scheduler_module.profile_crud, "lock_for_runtime_use", lock_profile)
    monkeypatch.setattr(scheduler_module.user_crud, "get_by_uid", get_user)
    monkeypatch.setattr(scheduler_module.session_reply_queue_manager, "enqueue_agent_work", enqueue_summary)
    monkeypatch.setattr(scheduler_module.message_crud, "persist", persist_message)

    await scheduler._dispatch_one_with_db(FakeDb(), scheduled_task)

    assert len(statements) == 2
    assert statements[0].get_execution_options()["synchronize_session"] is False
    assert statements[1].get_execution_options()["synchronize_session"] is False
