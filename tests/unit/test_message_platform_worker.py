import asyncio

import pytest

from app.workers import message_platform as worker


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

    monkeypatch.setattr(worker, "create_database_tables", create_tables)
    monkeypatch.setattr(worker.message_platform_polling_manager, "start", start_manager)
    monkeypatch.setattr(worker.message_platform_polling_manager, "stop", stop_manager)
    monkeypatch.setattr(worker.scheduled_task_scheduler, "start", start_scheduler)
    monkeypatch.setattr(worker.scheduled_task_scheduler, "stop", stop_scheduler)

    task = asyncio.create_task(worker.run_message_platform_worker())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == [
        "tables",
        "manager-start",
        "scheduler-start",
        "scheduler-stop",
        "manager-stop",
    ]
