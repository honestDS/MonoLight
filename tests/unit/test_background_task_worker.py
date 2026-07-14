import asyncio

import pytest

from app.workers import background_task as worker


@pytest.mark.asyncio
async def test_worker_recovers_then_starts_and_stops_manager_and_cleaners(monkeypatch):
    events = []

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

    async def run_with_lease(worker_name, stop_event, run_owned_worker):
        events.append(f"lease:{worker_name}")
        await run_owned_worker(stop_event)

    monkeypatch.setattr(worker, "install_shutdown_signal_handlers", lambda stop_event: None)
    monkeypatch.setattr(worker, "create_database_tables", create_tables)
    monkeypatch.setattr(worker, "run_with_worker_lease", run_with_lease)
    monkeypatch.setattr(worker, "recover_pending_background_tasks", recover_tasks)
    monkeypatch.setattr(worker, "recover_pending_background_task_replies", recover_replies)
    monkeypatch.setattr(worker.background_task_manager, "start", start_manager)
    monkeypatch.setattr(worker.background_task_manager, "stop", stop_manager)
    monkeypatch.setattr(worker, "background_log_cleaner", lambda days: cleaner("log-cleaner"))
    monkeypatch.setattr(worker, "background_temp_cleaner", lambda: cleaner("temp-cleaner"))
    monkeypatch.setattr(
        worker,
        "background_context_summary_cleaner",
        lambda: cleaner("context-summary-cleaner"),
    )

    task = asyncio.create_task(worker.run_background_task_worker())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == [
        "tables",
        "lease:background_task",
        "recover",
        "recover-replies",
        "manager-start",
        "log-cleaner-start",
        "temp-cleaner-start",
        "context-summary-cleaner-start",
        "manager-stop",
        "log-cleaner-stop",
        "temp-cleaner-stop",
        "context-summary-cleaner-stop",
    ]
