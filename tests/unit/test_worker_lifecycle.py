import asyncio
from types import SimpleNamespace

import pytest

from app.core.terminal.manager import TerminalWorkerCoordinator
from app.workers import background_task as background_task_worker
from app.workers import general as general_worker
from app.workers import memory as memory_worker
from app.workers import message_platform as message_platform_worker
from app.workers import session_reply as session_reply_worker
from app.workers import signals
from app.workers import terminal as terminal_worker


@pytest.mark.asyncio
async def test_background_task_worker_starts_and_stops_manager_and_cleaners(monkeypatch):
    events = []
    leased_stop_event = None

    async def create_tables():
        events.append("tables")

    async def clear_background_tasks_on_startup():
        events.append("clear")

    async def run_startup_cleanup(stop_event, *, worker_name: str, other_worker_name, cleanup):
        await cleanup()

    def start_manager():
        events.append("manager-start")

    async def stop_manager():
        events.append("manager-stop")

    async def cleaner(name):
        events.append(f"{name}-start")
        try:
            await asyncio.Event().wait()
        finally:
            events.append(f"{name}-stop")

    async def run_with_lease(worker_name, stop_event, run_owned_worker):
        nonlocal leased_stop_event
        leased_stop_event = stop_event
        events.append(f"lease:{worker_name}")
        await run_owned_worker(stop_event)

    monkeypatch.setattr(background_task_worker, "install_shutdown_signal_handlers", lambda stop_event: None)
    monkeypatch.setattr(background_task_worker, "create_database_tables", create_tables)
    monkeypatch.setattr(background_task_worker, "clear_background_tasks_on_startup", clear_background_tasks_on_startup)
    monkeypatch.setattr(background_task_worker, "run_startup_cleanup", run_startup_cleanup)
    monkeypatch.setattr(background_task_worker, "run_with_worker_lease", run_with_lease)
    monkeypatch.setattr(background_task_worker.background_task_manager, "start", start_manager)
    monkeypatch.setattr(background_task_worker.background_task_manager, "stop", stop_manager)
    monkeypatch.setattr(background_task_worker, "background_log_cleaner", lambda days: cleaner("log-cleaner"))
    monkeypatch.setattr(background_task_worker, "background_temp_cleaner", lambda: cleaner("temp-cleaner"))
    monkeypatch.setattr(
        background_task_worker,
        "background_context_summary_cleaner",
        lambda: cleaner("context-summary-cleaner"),
    )

    task = asyncio.create_task(background_task_worker.run_background_task_worker())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == [
        "tables",
        "lease:background_task",
        "clear",
        "manager-start",
        "log-cleaner-start",
        "temp-cleaner-start",
        "context-summary-cleaner-start",
        "manager-stop",
        "log-cleaner-stop",
        "temp-cleaner-stop",
        "context-summary-cleaner-stop",
    ]


@pytest.mark.asyncio
async def test_worker_starts_and_stops_scheduler_and_message_platform_manager(monkeypatch):
    events = []

    async def create_tables():
        events.append("tables")

    def start_manager():
        events.append("manager-start")

    async def stop_manager():
        events.append("manager-stop")

    def start_scheduler():
        events.append("scheduler-start")

    async def stop_scheduler():
        events.append("scheduler-stop")

    async def run_with_lease(worker_name, stop_event, run_owned_worker):
        events.append(f"lease:{worker_name}")
        await run_owned_worker(stop_event)

    monkeypatch.setattr(message_platform_worker, "install_shutdown_signal_handlers", lambda stop_event: None)
    monkeypatch.setattr(message_platform_worker, "create_database_tables", create_tables)
    monkeypatch.setattr(message_platform_worker, "run_with_worker_lease", run_with_lease)
    monkeypatch.setattr(message_platform_worker.message_platform_polling_manager, "start", start_manager)
    monkeypatch.setattr(message_platform_worker.message_platform_polling_manager, "stop", stop_manager)
    monkeypatch.setattr(message_platform_worker.scheduled_task_scheduler, "start", start_scheduler)
    monkeypatch.setattr(message_platform_worker.scheduled_task_scheduler, "stop", stop_scheduler)

    task = asyncio.create_task(message_platform_worker.run_message_platform_worker())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == [
        "tables",
        "lease:message_platform",
        "manager-start",
        "scheduler-start",
        "scheduler-stop",
        "manager-stop",
    ]


@pytest.mark.asyncio
async def test_terminal_worker_starts_and_stops_coordinator_inside_worker_lease(monkeypatch):
    events = []

    async def create_tables():
        events.append("tables")

    def start_coordinator():
        events.append("coordinator-start")

    async def stop_coordinator():
        events.append("coordinator-stop")

    async def run_with_lease(worker_name, stop_event, run_owned_worker):
        events.append(f"lease:{worker_name}")
        await run_owned_worker(stop_event)

    monkeypatch.setattr(terminal_worker, "install_shutdown_signal_handlers", lambda stop_event: None)
    monkeypatch.setattr(terminal_worker, "create_database_tables", create_tables)
    monkeypatch.setattr(terminal_worker, "run_with_worker_lease", run_with_lease)
    monkeypatch.setattr(terminal_worker.terminal_worker_coordinator, "start", start_coordinator)
    monkeypatch.setattr(terminal_worker.terminal_worker_coordinator, "stop", stop_coordinator)

    task = asyncio.create_task(terminal_worker.run_terminal_worker())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == [
        "tables",
        "lease:terminal",
        "coordinator-start",
        "coordinator-stop",
    ]


@pytest.mark.asyncio
async def test_memory_worker_starts_and_stops_memory_and_knowledge_job_consumers(monkeypatch):
    events = []
    captured_stop_event = None

    async def create_tables():
        events.append("tables")

    def install_shutdown_signal_handlers(stop_event):
        nonlocal captured_stop_event
        captured_stop_event = stop_event

    class FakeConsumer:
        def __init__(self, name):
            self.name = name

        def start(self):
            events.append(f"{self.name}-start")

        async def stop(self):
            events.append(f"{self.name}-stop")

    def create_memory_job_consumer():
        events.append("memory-create")
        return FakeConsumer("memory")

    def create_knowledge_job_consumer():
        events.append("knowledge-create")
        return FakeConsumer("knowledge")

    monkeypatch.setattr(memory_worker, "install_shutdown_signal_handlers", install_shutdown_signal_handlers)
    monkeypatch.setattr(memory_worker, "create_database_tables", create_tables)
    monkeypatch.setattr(memory_worker, "create_memory_job_consumer", create_memory_job_consumer)
    monkeypatch.setattr(memory_worker, "create_knowledge_job_consumer", create_knowledge_job_consumer)

    task = asyncio.create_task(memory_worker.run_memory_worker())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert captured_stop_event is not None
    captured_stop_event.set()
    await task

    assert events == [
        "tables",
        "memory-create",
        "knowledge-create",
        "memory-start",
        "knowledge-start",
        "knowledge-stop",
        "memory-stop",
    ]


@pytest.mark.asyncio
async def test_session_reply_worker_starts_and_stops_consumer_inside_worker_lease(monkeypatch):
    events = []
    consumer_started = asyncio.Event()
    captured_stop_event = None

    async def create_tables():
        events.append("tables")

    async def clear_background_tasks_on_startup():
        pass

    async def run_startup_cleanup(stop_event, *, worker_name: str, other_worker_name, cleanup):
        await cleanup()

    def install_shutdown_signal_handlers(stop_event):
        nonlocal captured_stop_event
        captured_stop_event = stop_event

    class FakeConsumer:
        def start(self):
            events.append("consumer-start")
            consumer_started.set()

        async def stop(self):
            events.append("consumer-stop")

    async def run_with_lease(worker_name, stop_event, run_owned_worker):
        events.append(f"lease:{worker_name}")
        await run_owned_worker(stop_event)

    monkeypatch.setattr(session_reply_worker, "install_shutdown_signal_handlers", install_shutdown_signal_handlers)
    monkeypatch.setattr(session_reply_worker, "create_database_tables", create_tables)
    monkeypatch.setattr(session_reply_worker, "clear_background_tasks_on_startup", clear_background_tasks_on_startup)
    monkeypatch.setattr(session_reply_worker, "run_startup_cleanup", run_startup_cleanup)
    monkeypatch.setattr(session_reply_worker, "run_with_worker_lease", run_with_lease)
    monkeypatch.setattr(session_reply_worker, "session_reply_consumer", FakeConsumer())

    task = asyncio.create_task(session_reply_worker.run_session_reply_worker())
    await asyncio.wait_for(consumer_started.wait(), timeout=1)
    assert captured_stop_event is not None
    captured_stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert events == [
        "tables",
        "lease:session_reply",
        "consumer-start",
        "consumer-stop",
    ]


@pytest.mark.asyncio
async def test_general_worker_runs_all_owned_workers_with_shared_stop_event(monkeypatch):
    create_tables_calls = 0
    signal_events = []
    lease_calls = []
    started_roles = []
    all_roles_started = asyncio.Event()
    background_ready = asyncio.Event()
    roles_entered = asyncio.Event()

    async def create_tables():
        nonlocal create_tables_calls
        create_tables_calls += 1

    def install_shutdown_signal_handlers(stop_event):
        signal_events.append(stop_event)

    def record_started_role(worker_name):
        started_roles.append(worker_name)
        if len(started_roles) == 3:
            all_roles_started.set()

    async def run_owned_background_task_worker(owned_stop_event, *, startup_ready):
        record_started_role("background_task")
        startup_ready.set()
        background_ready.set()
        await owned_stop_event.wait()

    async def run_owned_message_platform_worker(owned_stop_event):
        assert background_ready.is_set()
        record_started_role("message_platform")
        await owned_stop_event.wait()

    async def run_owned_session_reply_worker(owned_stop_event, *, startup_cleaned):
        assert startup_cleaned is True
        assert background_ready.is_set()
        record_started_role("session_reply")
        await owned_stop_event.wait()

    async def run_with_lease(worker_name, stop_event, run_owned_worker):
        lease_calls.append((worker_name, stop_event))
        if worker_name != "background_task":
            assert background_ready.is_set()
        if len(lease_calls) == 3:
            roles_entered.set()
        await run_owned_worker(stop_event)

    monkeypatch.setattr(general_worker, "create_database_tables", create_tables)
    monkeypatch.setattr(general_worker, "install_shutdown_signal_handlers", install_shutdown_signal_handlers)
    monkeypatch.setattr(general_worker, "run_owned_background_task_worker", run_owned_background_task_worker)
    monkeypatch.setattr(general_worker, "run_owned_message_platform_worker", run_owned_message_platform_worker)
    monkeypatch.setattr(general_worker, "run_owned_session_reply_worker", run_owned_session_reply_worker)
    monkeypatch.setattr(general_worker, "run_with_worker_lease", run_with_lease)

    task = asyncio.create_task(general_worker.run_general_worker())
    await asyncio.wait_for(all_roles_started.wait(), timeout=1)

    assert create_tables_calls == 1
    assert len(signal_events) == 1
    assert len(lease_calls) == 3
    shared_stop_event = signal_events[0]
    assert all(stop_event is shared_stop_event for _, stop_event in lease_calls)
    assert set(started_roles) == {
        "message_platform",
        "background_task",
        "session_reply",
    }
    assert {worker_name for worker_name, _ in lease_calls} == set(started_roles)
    assert roles_entered.is_set()

    shared_stop_event.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_general_worker_stops_other_roles_when_one_lease_returns(monkeypatch):
    entered_roles = []
    started_roles = []
    stopped_roles = []
    returned_roles = []
    all_roles_entered = asyncio.Event()
    all_roles_started = asyncio.Event()
    background_ready = asyncio.Event()
    signal_events = []

    async def create_tables():
        return None

    def install_shutdown_signal_handlers(stop_event):
        signal_events.append(stop_event)

    def record_started_role(worker_name):
        started_roles.append(worker_name)
        if len(started_roles) == 3:
            all_roles_started.set()

    async def run_owned_background_task_worker(owned_stop_event, *, startup_ready):
        record_started_role("background_task")
        startup_ready.set()
        background_ready.set()
        await owned_stop_event.wait()

    async def run_owned_message_platform_worker(owned_stop_event):
        assert background_ready.is_set()
        record_started_role("message_platform")
        await all_roles_started.wait()

    async def run_owned_session_reply_worker(owned_stop_event, *, startup_cleaned):
        assert startup_cleaned is True
        assert background_ready.is_set()
        record_started_role("session_reply")
        await owned_stop_event.wait()

    async def run_with_lease(worker_name, stop_event, run_owned_worker):
        entered_roles.append(worker_name)
        if worker_name != "background_task":
            assert background_ready.is_set()
        if len(entered_roles) == 3:
            all_roles_entered.set()
        await run_owned_worker(stop_event)
        if worker_name == "message_platform":
            returned_roles.append(worker_name)
        else:
            stopped_roles.append(worker_name)

    monkeypatch.setattr(general_worker, "create_database_tables", create_tables)
    monkeypatch.setattr(general_worker, "install_shutdown_signal_handlers", install_shutdown_signal_handlers)
    monkeypatch.setattr(general_worker, "run_owned_background_task_worker", run_owned_background_task_worker)
    monkeypatch.setattr(general_worker, "run_owned_message_platform_worker", run_owned_message_platform_worker)
    monkeypatch.setattr(general_worker, "run_owned_session_reply_worker", run_owned_session_reply_worker)
    monkeypatch.setattr(general_worker, "run_with_worker_lease", run_with_lease)

    task = asyncio.create_task(general_worker.run_general_worker())
    await asyncio.wait_for(all_roles_entered.wait(), timeout=1)
    await asyncio.wait_for(all_roles_started.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)

    assert set(entered_roles) == {
        "message_platform",
        "background_task",
        "session_reply",
    }
    assert set(started_roles) == set(entered_roles)
    assert returned_roles == ["message_platform"]
    assert len(signal_events) == 1
    assert signal_events[0].is_set()
    assert set(stopped_roles) == {"background_task", "session_reply"}


@pytest.mark.asyncio
async def test_terminal_worker_coordinator_can_restart_after_stop(monkeypatch):
    coordinator = TerminalWorkerCoordinator()
    run_count = 0

    async def run():
        nonlocal run_count
        run_count += 1
        await coordinator._stop_event.wait()

    monkeypatch.setattr(coordinator, "_run", run)

    coordinator.start()
    await coordinator.stop()
    assert coordinator._task is None

    coordinator.start()
    await coordinator.stop()
    assert coordinator._task is None
    assert run_count == 2


def test_install_shutdown_signal_handlers_falls_back_to_signal_module(monkeypatch):
    stop_event = asyncio.Event()
    shutdown_signal = object()
    registered_handlers = []

    class Loop:
        def add_signal_handler(self, received_signal, callback):
            assert received_signal is shutdown_signal
            raise NotImplementedError

        def call_soon_threadsafe(self, callback):
            callback()

    monkeypatch.setattr(signals.asyncio, "get_running_loop", lambda: Loop())
    monkeypatch.setattr(
        signals,
        "signal",
        SimpleNamespace(
            SIGINT=None,
            SIGTERM=None,
            SIGBREAK=shutdown_signal,
            signal=lambda received_signal, callback: registered_handlers.append((received_signal, callback)),
        ),
    )

    signals.install_shutdown_signal_handlers(stop_event)
    registered_handlers[0][1]()

    assert registered_handlers[0][0] is shutdown_signal
    assert stop_event.is_set()
