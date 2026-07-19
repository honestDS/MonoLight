import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.background_tasks import runner as runner_module


class SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


class TrackingSession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def refresh(self, _value):
        return None


class CapturingLog:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def bind(self, **kwargs):
        return self

    def info(self, message):
        return None

    def warning(self, message):
        self.warnings.append(message)

    def error(self, message, *, exc_info=False):
        self.errors.append((message, exc_info))


def test_to_json_compatible_preserves_nested_structure():
    created_at = datetime(2026, 7, 11, 1, 2, 3, tzinfo=UTC)
    value = {
        "metadata": {
            "created_at": created_at,
            "path": Path("temp/result.txt"),
        },
        "items": (1, Path("temp/one.txt")),
        7: {"values": {2, 1}},
    }

    normalized = runner_module._to_json_compatible(value)

    assert normalized["metadata"] == {
        "created_at": str(created_at),
        "path": str(Path("temp/result.txt")),
    }
    assert normalized["items"] == [1, str(Path("temp/one.txt"))]
    assert sorted(normalized["7"]["values"]) == [1, 2]
    json.dumps(normalized, ensure_ascii=False)


def test_to_json_compatible_replaces_circular_references():
    value = {"name": "result"}
    value["self"] = value

    normalized = runner_module._to_json_compatible(value)

    assert normalized == {
        "name": "result",
        "self": "<circular reference>",
    }
    json.dumps(normalized, ensure_ascii=False)


def test_limit_result_content_replaces_oversized_structure_with_valid_metadata(monkeypatch):
    content = {
        "items": [
            {
                "id": 1,
                "value": "oversized",
            }
        ]
    }
    serialized = json.dumps(content, ensure_ascii=False)
    monkeypatch.setattr(runner_module, "MAX_BACKGROUND_TASK_RESULT_CHARS", len(serialized) - 1)

    limited = runner_module._limit_result_content(content)

    assert limited == {
        "truncated": True,
        "original_chars": len(serialized),
    }
    assert json.loads(json.dumps(limited, ensure_ascii=False)) == limited


@pytest.mark.asyncio
async def test_renew_task_lease_retries_transient_database_error(monkeypatch):
    renew_calls = 0
    sleep_calls = []
    monotonic_values = iter([0, 1, 2])
    log = CapturingLog()

    async def sleep(seconds):
        sleep_calls.append(seconds)

    async def renew_lease(db, **kwargs):
        nonlocal renew_calls
        renew_calls += 1
        if renew_calls == 1:
            raise RuntimeError("temporary database failure")
        return renew_calls == 2

    monkeypatch.setattr(runner_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runner_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(runner_module, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(runner_module.background_task_crud, "renew_lease", renew_lease)

    renewed = await runner_module._renew_task_lease(7, "worker-a", log)

    assert renewed is False
    assert renew_calls == 3
    assert sleep_calls == [
        runner_module.BACKGROUND_TASK_LEASE_RENEW_INTERVAL_SECONDS,
        1.0,
        runner_module.BACKGROUND_TASK_LEASE_RENEW_INTERVAL_SECONDS,
    ]
    assert len(log.errors) == 1
    assert log.errors[0][1] is True
    assert len(log.warnings) == 1


@pytest.mark.asyncio
async def test_lost_lease_cancels_execution_and_releases_claim(monkeypatch):
    execution_started = asyncio.Event()
    execution_cancelled = asyncio.Event()
    released = []
    task = SimpleNamespace(
        id=9,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        tool_name="execute_shell",
        lock_until=123,
    )

    async def try_claim(db, **kwargs):
        return task

    async def execute_claimed(task_id, worker_id, log):
        execution_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            execution_cancelled.set()

    async def renew_task_lease(task_id, worker_id, log):
        await execution_started.wait()
        return False

    async def release_task_claim(task_id, worker_id, log, expected_lock_until=None):
        released.append((task_id, worker_id, expected_lock_until))

    monkeypatch.setattr(runner_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runner_module.background_task_crud, "try_claim", try_claim)
    monkeypatch.setattr(runner_module, "_execute_claimed_background_task", execute_claimed)
    monkeypatch.setattr(runner_module, "_renew_task_lease", renew_task_lease)
    monkeypatch.setattr(runner_module, "_release_task_claim", release_task_claim)

    await runner_module.run_background_task(task.id, worker_id="worker-a")

    assert execution_cancelled.is_set()
    assert released == [(task.id, "worker-a", None)]


@pytest.mark.asyncio
async def test_cancelled_background_runner_releases_claim_without_stale_lock(monkeypatch):
    execution_started = asyncio.Event()
    lease_started = asyncio.Event()
    lease_completed = asyncio.Event()
    events = []
    released = []
    task = SimpleNamespace(
        id=9,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        tool_name="execute_shell",
        lock_until=123,
    )

    async def try_claim(db, **kwargs):
        return task

    async def execute_claimed(task_id, worker_id, log):
        execution_started.set()
        await asyncio.Event().wait()

    async def renew_task_lease(task_id, worker_id, log):
        lease_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("lease_cancelled")
            raise
        finally:
            events.append("lease_completed")
            lease_completed.set()

    async def release_task_claim(task_id, worker_id, log, expected_lock_until=None):
        assert lease_completed.is_set()
        events.append("release")
        released.append((task_id, worker_id, expected_lock_until))

    monkeypatch.setattr(runner_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(runner_module.background_task_crud, "try_claim", try_claim)
    monkeypatch.setattr(runner_module, "_execute_claimed_background_task", execute_claimed)
    monkeypatch.setattr(runner_module, "_renew_task_lease", renew_task_lease)
    monkeypatch.setattr(runner_module, "_release_task_claim", release_task_claim)

    task_runner = asyncio.create_task(runner_module.run_background_task(task.id, worker_id="worker-a"))
    await execution_started.wait()
    await lease_started.wait()
    task_runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_runner

    assert released == [(task.id, "worker-a", None)]
    assert events == ["lease_cancelled", "lease_completed", "release"]


@pytest.mark.asyncio
async def test_background_tool_releases_database_connection_before_execution(monkeypatch):
    db = TrackingSession()
    task = SimpleNamespace(
        id=9,
        uid="user-1",
        session_id="session-1",
        profile_id=3,
        tool_name="generate_image",
        arguments={"prompt": "sunrise"},
        extra={},
        status="running",
        auto_reply=False,
    )
    profile = SimpleNamespace(id=3, uid="user-1", configs={})
    execution_commit_counts = []

    class Executor:
        def __init__(self, **_kwargs):
            return None

        def set_config(self, _cfg):
            return None

        def set_runtime_context(self, **_kwargs):
            return None

        async def execute(self, **_kwargs):
            execution_commit_counts.append(db.commits)
            return {"ok": True}

    async def get_task(_db, _task_id):
        return task

    async def get_profile(_db, _profile_id):
        return profile

    async def mark_succeeded(_db, **_kwargs):
        return True

    monkeypatch.setattr(runner_module, "AsyncSessionLocal", lambda: _SingleSessionContext(db))
    monkeypatch.setattr(runner_module.background_task_crud, "get", get_task)
    monkeypatch.setattr(runner_module.profile_crud, "get", get_profile)
    monkeypatch.setattr(runner_module.background_task_crud, "mark_succeeded", mark_succeeded)
    monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, "generate_image", Executor)

    result = await runner_module._execute_claimed_background_task(task.id, "worker-a", CapturingLog())

    assert result is True
    assert execution_commit_counts == [1]


class _SingleSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None
