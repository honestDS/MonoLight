import pytest
from fastapi import FastAPI

import main


@pytest.mark.asyncio
async def test_lifespan_creates_tables_before_starting_database_pollers(monkeypatch):
    events = []

    async def create_tables():
        events.append("create_tables")

    async def start_session_notifier():
        events.append("start_session_notifier")

    async def stop_session_notifier():
        events.append("stop_session_notifier")

    async def start_log_broadcaster():
        events.append("start_log_broadcaster")

    async def stop_log_broadcaster():
        events.append("stop_log_broadcaster")

    monkeypatch.setattr(main, "create_database_tables", create_tables)
    monkeypatch.setattr(main.session_notifier, "start", start_session_notifier)
    monkeypatch.setattr(main.session_notifier, "stop", stop_session_notifier)
    monkeypatch.setattr(main.log_broadcaster, "start", start_log_broadcaster)
    monkeypatch.setattr(main.log_broadcaster, "stop", stop_log_broadcaster)
    monkeypatch.setattr(main.LogManager, "setup", lambda **_kwargs: None)
    monkeypatch.setattr(main, "get_logger", lambda _name: type("Logger", (), {"info": lambda self, _message: None})())

    async with main.lifespan(FastAPI()):
        events.append("running")

    assert events == [
        "create_tables",
        "start_session_notifier",
        "start_log_broadcaster",
        "running",
        "stop_log_broadcaster",
        "stop_session_notifier",
    ]
