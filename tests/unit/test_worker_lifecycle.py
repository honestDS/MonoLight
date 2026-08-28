import asyncio
from types import SimpleNamespace

import pytest

from app.core.terminal.manager import TerminalWorkerCoordinator
from app.workers import background_task as background_task_worker
from app.workers import memory as memory_worker
from app.workers import message_platform as message_platform_worker
from app.workers import signals
from app.workers import terminal as terminal_worker


@pytest.mark.asyncio
async def test_worker_recovers_then_starts_and_stops_manager_and_cleaners(monkeypatch):
    events = []
    captured_cleanup_stop_event = None
    leased_stop_event = None

    async def create_tables():
        events.append("tables")

    async def recover_tasks():
        events.append("recover")

    async def recover_replies():
        events.append("recover-replies")

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

    async def collection_cleanup_loop(stop_event):
        nonlocal captured_cleanup_stop_event
        captured_cleanup_stop_event = stop_event
        await cleaner("collection-cleanup")

    async def run_with_lease(worker_name, stop_event, run_owned_worker):
        nonlocal leased_stop_event
        leased_stop_event = stop_event
        events.append(f"lease:{worker_name}")
        await run_owned_worker(stop_event)

    monkeypatch.setattr(background_task_worker, "install_shutdown_signal_handlers", lambda stop_event: None)
    monkeypatch.setattr(background_task_worker, "create_database_tables", create_tables)
    monkeypatch.setattr(background_task_worker, "run_with_worker_lease", run_with_lease)
    monkeypatch.setattr(background_task_worker, "recover_pending_background_tasks", recover_tasks)
    monkeypatch.setattr(background_task_worker, "recover_pending_background_task_replies", recover_replies)
    monkeypatch.setattr(background_task_worker.background_task_manager, "start", start_manager)
    monkeypatch.setattr(background_task_worker.background_task_manager, "stop", stop_manager)
    monkeypatch.setattr(background_task_worker, "background_log_cleaner", lambda days: cleaner("log-cleaner"))
    monkeypatch.setattr(background_task_worker, "background_temp_cleaner", lambda: cleaner("temp-cleaner"))
    monkeypatch.setattr(
        background_task_worker,
        "background_context_summary_cleaner",
        lambda: cleaner("context-summary-cleaner"),
    )
    monkeypatch.setattr(
        background_task_worker,
        "run_knowledge_base_collection_cleanup_loop",
        collection_cleanup_loop,
    )

    task = asyncio.create_task(background_task_worker.run_background_task_worker())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert captured_cleanup_stop_event is leased_stop_event
    assert events == [
        "tables",
        "lease:background_task",
        "recover",
        "recover-replies",
        "manager-start",
        "log-cleaner-start",
        "temp-cleaner-start",
        "context-summary-cleaner-start",
        "collection-cleanup-start",
        "manager-stop",
        "log-cleaner-stop",
        "temp-cleaner-stop",
        "context-summary-cleaner-stop",
        "collection-cleanup-stop",
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
