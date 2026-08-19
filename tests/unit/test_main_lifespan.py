import httpx
import pytest
from fastapi import FastAPI

import main


def test_register_dashboard_skips_mount_when_index_is_missing(tmp_path):
    app = FastAPI()

    main.register_dashboard(app, tmp_path)

    assert "dashboard" not in {route.name for route in app.routes}


@pytest.mark.asyncio
async def test_register_dashboard_serves_index_and_preserves_prior_api_route(tmp_path):
    index_html = "<html><body>Dashboard</body></html>"
    (tmp_path / "index.html").write_text(index_html, encoding="utf-8")
    app = FastAPI()

    @app.get("/api/test")
    async def test_api_route():
        return {"status": "ok"}

    main.register_dashboard(app, tmp_path)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        index_response = await client.get("/")
        api_response = await client.get("/api/test")

    assert index_response.status_code == 200
    assert index_response.text == index_html
    assert api_response.status_code == 200
    assert api_response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_lifespan_creates_tables_before_starting_database_pollers(monkeypatch):
    events = []
    session = object()

    class SessionContext:
        async def __aenter__(self):
            events.append("enter_database_session")
            return session

        async def __aexit__(self, exc_type, exc_value, traceback):
            events.append("exit_database_session")
            return False

    async def init_database_schema(database_session):
        assert database_session is session
        events.append("init_database_schema")

    async def start_session_notifier():
        events.append("start_session_notifier")

    async def stop_session_notifier():
        events.append("stop_session_notifier")

    async def start_log_broadcaster():
        events.append("start_log_broadcaster")

    async def stop_log_broadcaster():
        events.append("stop_log_broadcaster")

    monkeypatch.setattr(main, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(main, "init_database_schema", init_database_schema)
    monkeypatch.setattr(main.session_notifier, "start", start_session_notifier)
    monkeypatch.setattr(main.session_notifier, "stop", stop_session_notifier)
    monkeypatch.setattr(main.log_broadcaster, "start", start_log_broadcaster)
    monkeypatch.setattr(main.log_broadcaster, "stop", stop_log_broadcaster)
    monkeypatch.setattr(main.LogManager, "setup", lambda **_kwargs: None)
    monkeypatch.setattr(main, "get_logger", lambda _name: type("Logger", (), {"info": lambda self, _message: None})())

    async with main.lifespan(FastAPI()):
        events.append("running")

    assert events == [
        "enter_database_session",
        "init_database_schema",
        "exit_database_session",
        "start_session_notifier",
        "start_log_broadcaster",
        "running",
        "stop_log_broadcaster",
        "stop_session_notifier",
    ]
