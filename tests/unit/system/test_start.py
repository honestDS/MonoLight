import asyncio
import secrets
import subprocess
import sys
from types import SimpleNamespace

import pytest

import start
from app.models.system_setting import SystemRuntimeSettings


@pytest.fixture(autouse=True)
def isolate_notification_config(monkeypatch):
    from app.core.notifications.config import NotificationConfig

    config = NotificationConfig(
        host="127.0.0.1",
        port=8766,
        token=secrets.token_hex(32),
        server_host=None,
    )
    monkeypatch.setattr(start, "load_notification_config", lambda: config)
    return config


@pytest.fixture
def notification_ready_path(tmp_path):
    ready_path = tmp_path / "notification-ready"
    ready_path.write_text("", encoding="utf-8")
    return ready_path


class ExitedProcess:
    def __init__(self, return_code: int | None, pid: int = 1234) -> None:
        self.return_code = return_code
        self.pid = pid

    def poll(self) -> int | None:
        return self.return_code


def _stub_notification_client(monkeypatch, ping):
    from app.core.notifications.config import NotificationConfig

    config = NotificationConfig(host="127.0.0.1", port=8766, token=secrets.token_hex(32))
    monkeypatch.setattr(start, "load_notification_config", lambda: config)
    monkeypatch.setattr(start.NotificationClient, "ping", ping)


def test_system_runtime_settings_default_audit_retention_is_ninety_days():
    assert SystemRuntimeSettings().audit_retention_days == 90


def test_system_runtime_settings_reserves_audit_report_email():
    settings = SystemRuntimeSettings(audit_report_email="  audit@example.com  ").normalized()

    assert settings.audit_report_email == "audit@example.com"


@pytest.mark.asyncio
async def test_initialize_system_recovers_audit_state_after_initialization(monkeypatch):
    from app.core.audit import startup as audit_startup_module
    from app.providers import database as database_module
    from app.providers.database import bootstrap as bootstrap_module

    events = []
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def init_system_data(received_session):
        events.append(("initialize", received_session))

    async def recover_audit_state(received_session):
        events.append(("recover", received_session))

    monkeypatch.setattr(start, "load_dotenv", lambda: events.append("dotenv"))
    monkeypatch.setattr(start, "initialize_system_secrets", lambda: events.append("system_secrets"))
    monkeypatch.setattr(database_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(bootstrap_module, "init_system_data", init_system_data)
    monkeypatch.setattr(audit_startup_module, "recover_audit_state", recover_audit_state)

    await start.initialize_system()

    assert events == [
        "dotenv",
        "system_secrets",
        ("initialize", session),
        ("recover", session),
    ]


def test_load_start_config_reads_worker_count_from_environment(monkeypatch):
    monkeypatch.setattr(start, "load_dotenv", lambda: None)
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    monkeypatch.setenv("APP_PORT", "9000")
    monkeypatch.setenv("APP_WORKERS", "3")

    config = start.load_start_config()

    assert config == start.StartConfig(host="127.0.0.1", port=9000, web_workers=3)


@pytest.mark.parametrize("app_host", [None, "   "])
def test_load_start_config_defaults_missing_or_blank_app_host(monkeypatch, app_host):
    monkeypatch.setattr(start, "load_dotenv", lambda: None)
    monkeypatch.setenv("APP_PORT", "8000")
    monkeypatch.setenv("APP_WORKERS", "1")
    if app_host is None:
        monkeypatch.delenv("APP_HOST", raising=False)
    else:
        monkeypatch.setenv("APP_HOST", app_host)

    config = start.load_start_config()

    assert config.host == "0.0.0.0"


@pytest.mark.parametrize(
    ("app_host", "expected_host"),
    [
        ("192.0.2.10", "192.0.2.10"),
        ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
    ],
)
def test_load_start_config_accepts_and_normalizes_app_host(monkeypatch, app_host, expected_host):
    monkeypatch.setattr(start, "load_dotenv", lambda: None)
    monkeypatch.setenv("APP_PORT", "8000")
    monkeypatch.setenv("APP_WORKERS", "1")
    monkeypatch.setenv("APP_HOST", app_host)

    config = start.load_start_config()

    assert config.host == expected_host


@pytest.mark.parametrize(
    "app_host",
    [
        "example.com",
        "http://127.0.0.1:8000",
        "127.0.0.1:8000",
        "::",
        "0:0:0:0:0:0:0:0",
        "fe80::1%eth0",
    ],
)
def test_load_start_config_rejects_invalid_app_host(monkeypatch, app_host):
    monkeypatch.setattr(start, "load_dotenv", lambda: None)
    monkeypatch.setenv("APP_HOST", app_host)

    with pytest.raises(ValueError, match="APP_HOST"):
        start.load_start_config()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_PORT", "0"),
        ("APP_PORT", "65536"),
        ("APP_PORT", "invalid"),
        ("APP_WORKERS", "0"),
        ("APP_WORKERS", "invalid"),
    ],
)
def test_load_start_config_rejects_invalid_numeric_values(monkeypatch, name, value):
    monkeypatch.setattr(start, "load_dotenv", lambda: None)
    monkeypatch.setenv("APP_PORT", "8000")
    monkeypatch.setenv("APP_WORKERS", "1")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        start.load_start_config()


def test_build_web_command_uses_current_python_and_worker_count():
    config = start.StartConfig(host="0.0.0.0", port=8001, web_workers=4)

    command = start.build_web_command(config)

    assert command == [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8001",
        "--workers",
        "4",
        "--loop",
        "app.core.event_loop:create_windows_selector_event_loop" if sys.platform == "win32" else "auto",
    ]


def test_windows_selector_event_loop_factory_returns_closable_selector_loop():
    from app.core.event_loop import create_windows_selector_event_loop

    loop = create_windows_selector_event_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


@pytest.mark.parametrize(
    ("command_builder", "module_name"),
    [
        (start.build_notification_command, "app.workers.notification"),
        (start.build_general_command, "app.workers.general"),
        (start.build_message_platform_command, "app.workers.message_platform"),
        (start.build_background_task_command, "app.workers.background_task"),
        (start.build_memory_command, "app.workers.memory"),
        (start.build_terminal_command, "app.workers.terminal"),
        (start.build_session_reply_command, "app.workers.session_reply"),
    ],
)
def test_build_worker_command_starts_exactly_one_worker_process(command_builder, module_name):
    assert command_builder() == [
        sys.executable,
        "-m",
        module_name,
    ]


def test_build_initialize_command_uses_current_python_and_initialize_code():
    assert start.build_initialize_command() == [
        sys.executable,
        "-c",
        start.INITIALIZE_SYSTEM_CODE,
    ]


def test_run_system_initialization_runs_initialize_subprocess(monkeypatch):
    environment = object()
    calls = []

    def run_process(command, *, env, check):
        calls.append((command, env, check))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(start.subprocess, "run", run_process)

    start.run_system_initialization(environment)

    assert calls == [(start.build_initialize_command(), environment, False)]


def test_run_system_initialization_raises_for_failed_initialize_subprocess(monkeypatch):
    environment = object()

    def run_process(command, *, env, check):
        return subprocess.CompletedProcess(command, 23)

    monkeypatch.setattr(start.subprocess, "run", run_process)

    with pytest.raises(RuntimeError, match="23"):
        start.run_system_initialization(environment)


def test_report_process_started_includes_name_and_pid(capsys):
    process = ExitedProcess(return_code=None, pid=4321)

    start.report_process_started("Background task worker", process)

    assert capsys.readouterr().out == "Background task worker process started [PID 4321]\n"


@pytest.mark.parametrize(
    ("config", "expected_url"),
    [
        (start.StartConfig(host="0.0.0.0", port=8001, web_workers=1), "http://127.0.0.1:8001/"),
        (start.StartConfig(host="192.0.2.10", port=9123, web_workers=1), "http://192.0.2.10:9123/"),
        (start.StartConfig(host="::1", port=9123, web_workers=1), "http://[::1]:9123/"),
    ],
)
def test_build_access_url_uses_local_accessible_host(config, expected_url):
    assert start.build_access_url(config) == expected_url


def test_report_access_url_prints_english_dashboard_log(capsys, monkeypatch):
    config = start.StartConfig(host="0.0.0.0", port=8001, web_workers=1)
    monkeypatch.setattr(start, "_should_color_access_url", lambda: False)

    start.report_access_url(config)

    assert capsys.readouterr().out == "Dashboard access URL: http://127.0.0.1:8001/ you can access it in your browser or use.\n"


def test_validate_dashboard_assets_accepts_existing_index(tmp_path, monkeypatch):
    index_path = tmp_path / "index.html"
    index_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(start, "DASHBOARD_INDEX_PATH", index_path)

    start.validate_dashboard_assets()


def test_validate_dashboard_assets_rejects_missing_index(tmp_path, monkeypatch):
    index_path = tmp_path / "index.html"
    monkeypatch.setattr(start, "DASHBOARD_INDEX_PATH", index_path)

    with pytest.raises(RuntimeError) as exc_info:
        start.validate_dashboard_assets()

    assert str(index_path) in str(exc_info.value)


def test_run_initializes_system_before_starting_processes(monkeypatch):
    events = []
    processes = []
    popen_environments = []
    ready_paths = []
    stop_directory_exists = []

    monkeypatch.delenv(start.NOTIFICATION_READY_FILE_ENV, raising=False)

    def run_system_initialization(environment):
        events.append("initialize")

    def start_process(command, *args, **kwargs):
        events.append(("process", command[2]))
        popen_environments.append(kwargs["env"])
        process = ExitedProcess(return_code=1)
        processes.append(process)
        return process

    def notification_ready(process, notification_config, *, ready_path=None):
        ready_paths.append(ready_path)
        assert ready_path.parent.is_dir()
        assert not ready_path.exists()
        events.append(("notification-ready", process))

    def stop_processes(running_processes):
        stop_directory_exists.append(ready_paths[0].parent.is_dir())

    monkeypatch.setattr(start, "load_start_config", lambda: start.StartConfig(host="127.0.0.1", port=8000, web_workers=2))
    monkeypatch.setattr(start, "validate_dashboard_assets", lambda: events.append("dashboard"))
    monkeypatch.setattr(start, "run_system_initialization", run_system_initialization)
    monkeypatch.setattr(start.subprocess, "Popen", start_process)
    monkeypatch.setattr(start, "wait_for_notification_service", notification_ready)
    monkeypatch.setattr(start, "wait_for_web_service", lambda process, config: events.append(("web-ready", process)))
    monkeypatch.setattr(start, "report_access_url", lambda config: events.append("access_url"))
    monkeypatch.setattr(start, "stop_processes", stop_processes)

    return_code = start.run()

    assert return_code == 1
    assert len(processes) == 5
    assert events == [
        "dashboard",
        "initialize",
        ("process", "app.workers.notification"),
        ("notification-ready", processes[0]),
        ("process", "uvicorn"),
        ("web-ready", processes[1]),
        ("process", "app.workers.general"),
        ("process", "app.workers.memory"),
        ("process", "app.workers.terminal"),
        "access_url",
    ]
    assert len(popen_environments) == 5
    assert start.NOTIFICATION_READY_FILE_ENV in popen_environments[0]
    assert [start.NOTIFICATION_READY_FILE_ENV in environment for environment in popen_environments[1:]] == [False] * 4
    assert popen_environments[0][start.NOTIFICATION_READY_FILE_ENV] == str(ready_paths[0])
    assert stop_directory_exists == [True]
    assert not ready_paths[0].parent.exists()


def test_run_cleans_up_notification_process_when_notification_startup_fails(monkeypatch):
    notification_process = ExitedProcess(return_code=None)
    previous_handlers = {start.signal.SIGTERM: object()}
    started_commands = []
    stopped_processes = []
    restored_handlers = []

    def start_process(command, **kwargs):
        started_commands.append(command)
        return notification_process

    ready_paths = []

    def fail_notification_start(process, notification_config, *, ready_path=None):
        ready_paths.append(ready_path)
        raise RuntimeError("notification startup failed")

    monkeypatch.setattr(start, "load_start_config", lambda: start.StartConfig(host="127.0.0.1", port=8000, web_workers=1))
    monkeypatch.setattr(start, "validate_dashboard_assets", lambda: None)
    monkeypatch.setattr(start, "run_system_initialization", lambda environment: None)
    monkeypatch.setattr(start.subprocess, "Popen", start_process)
    monkeypatch.setattr(start, "_install_shutdown_signal_handlers", lambda: previous_handlers)
    monkeypatch.setattr(start, "_restore_signal_handlers", restored_handlers.append)
    monkeypatch.setattr(start, "stop_processes", lambda processes: stopped_processes.append(list(processes)))
    monkeypatch.setattr(start, "wait_for_notification_service", fail_notification_start)

    with pytest.raises(RuntimeError, match="notification startup failed"):
        start.run()

    assert started_commands == [start.build_notification_command()]
    assert stopped_processes == [[notification_process]]
    assert restored_handlers == [previous_handlers]
    assert len(ready_paths) == 1
    assert not ready_paths[0].parent.exists()


def test_run_monitors_and_cleans_up_notification_process(monkeypatch):
    processes = []
    stopped_processes = []

    def start_process(command, **kwargs):
        process = ExitedProcess(return_code=None)
        processes.append(process)
        return process

    def notification_becomes_ready(process, notification_config, *, ready_path=None):
        process.return_code = 17

    monkeypatch.setattr(start, "load_start_config", lambda: start.StartConfig(host="127.0.0.1", port=8000, web_workers=1))
    monkeypatch.setattr(start, "validate_dashboard_assets", lambda: None)
    monkeypatch.setattr(start, "run_system_initialization", lambda environment: None)
    monkeypatch.setattr(start.subprocess, "Popen", start_process)
    monkeypatch.setattr(start, "wait_for_notification_service", notification_becomes_ready)
    monkeypatch.setattr(start, "wait_for_web_service", lambda process, config: None)
    monkeypatch.setattr(start, "report_access_url", lambda config: None)
    monkeypatch.setattr(start, "stop_processes", lambda running_processes: stopped_processes.append(list(running_processes)))

    return_code = start.run()

    assert return_code == 17
    assert len(processes) == 5
    assert stopped_processes == [processes]


def test_run_rejects_missing_dashboard_before_initializing_or_starting_processes(tmp_path, monkeypatch):
    events = []
    index_path = tmp_path / "index.html"
    validate_dashboard_assets = start.validate_dashboard_assets

    def validate_dashboard():
        events.append("dashboard")
        validate_dashboard_assets()

    def run_system_initialization(environment):
        events.append("initialize")

    def start_process(*args, **kwargs):
        events.append("process")
        return ExitedProcess(return_code=1)

    monkeypatch.setattr(start, "DASHBOARD_INDEX_PATH", index_path)
    monkeypatch.setattr(start, "load_start_config", lambda: start.StartConfig(host="127.0.0.1", port=8000, web_workers=1))
    monkeypatch.setattr(start, "validate_dashboard_assets", validate_dashboard)
    monkeypatch.setattr(start, "run_system_initialization", run_system_initialization)
    monkeypatch.setattr(start.subprocess, "Popen", start_process)

    with pytest.raises(RuntimeError) as exc_info:
        start.run()

    assert str(index_path) in str(exc_info.value)
    assert events == ["dashboard"]


def test_run_with_shared_notification_center_starts_only_local_services(monkeypatch):
    from app.core.notifications.config import NotificationConfig

    notification_config = NotificationConfig(
        host="127.0.0.1",
        port=8766,
        token=secrets.token_hex(32),
        server_host="192.0.2.20",
    )
    web_config = start.StartConfig(host="127.0.0.1", port=8000, web_workers=1)
    processes = []
    commands = []
    events = []
    stopped_processes = []

    def start_process(command, **kwargs):
        commands.append(command)
        process = ExitedProcess(return_code=19 if command == start.build_terminal_command() else None)
        processes.append(process)
        events.append(("spawn", command))
        return process

    def wait_for_notification(process, config, *, ready_path=None):
        events.append(("notification-ready", process, config))

    def wait_for_web(process, config):
        events.append(("web-ready", process, config))

    monkeypatch.setattr(start, "load_start_config", lambda: web_config)
    monkeypatch.setattr(start, "load_notification_config", lambda: notification_config)
    monkeypatch.setattr(start, "validate_dashboard_assets", lambda: None)
    monkeypatch.setattr(start, "run_system_initialization", lambda environment: None)
    monkeypatch.setattr(start.subprocess, "Popen", start_process)
    monkeypatch.setattr(start, "TemporaryDirectory", lambda **kwargs: pytest.fail("remote notification center must not allocate a local ready directory"))
    monkeypatch.setattr(start, "wait_for_notification_service", wait_for_notification)
    monkeypatch.setattr(start, "wait_for_web_service", wait_for_web)
    monkeypatch.setattr(start, "report_access_url", lambda config: None)
    monkeypatch.setattr(start, "stop_processes", lambda running: stopped_processes.append(list(running)))

    return_code = start.run()

    assert return_code == 19
    assert commands == [
        start.build_web_command(web_config),
        start.build_general_command(),
        start.build_memory_command(),
        start.build_terminal_command(),
    ]
    assert events[0] == ("notification-ready", None, notification_config)
    assert stopped_processes == [processes]
    assert len(processes) == 4


def test_run_with_shared_notification_center_cleans_up_after_readiness_failure(monkeypatch):
    from app.core.notifications.config import NotificationConfig

    notification_config = NotificationConfig(
        host="127.0.0.1",
        port=8766,
        token=secrets.token_hex(32),
        server_host="192.0.2.20",
    )
    previous_handlers = {start.signal.SIGTERM: object()}
    popen_calls = []
    stopped_processes = []
    restored_handlers = []
    readiness_calls = []

    def wait_for_notification(process, config, *, ready_path=None):
        readiness_calls.append((process, config))
        raise RuntimeError("shared notification center unavailable")

    monkeypatch.setattr(start, "load_notification_config", lambda: notification_config)
    monkeypatch.setattr(start, "validate_dashboard_assets", lambda: None)
    monkeypatch.setattr(start, "run_system_initialization", lambda environment: None)
    monkeypatch.setattr(start.subprocess, "Popen", lambda *args, **kwargs: popen_calls.append((args, kwargs)))
    monkeypatch.setattr(start, "_install_shutdown_signal_handlers", lambda: previous_handlers)
    monkeypatch.setattr(start, "_restore_signal_handlers", restored_handlers.append)
    monkeypatch.setattr(start, "stop_processes", lambda processes: stopped_processes.append(list(processes)))
    monkeypatch.setattr(start, "wait_for_notification_service", wait_for_notification)

    with pytest.raises(RuntimeError, match="shared notification center unavailable"):
        start.run()

    assert readiness_calls == [(None, notification_config)]
    assert popen_calls == []
    assert stopped_processes == [[]]
    assert restored_handlers == [previous_handlers]


def test_run_rejects_invalid_notification_config_before_starting_processes(monkeypatch):
    popen_calls = []

    def load_invalid_config():
        raise ValueError("invalid notification configuration")

    monkeypatch.setattr(start, "validate_dashboard_assets", lambda: None)
    monkeypatch.setattr(start, "run_system_initialization", lambda environment: None)
    monkeypatch.setattr(start, "load_notification_config", load_invalid_config)
    monkeypatch.setattr(start.subprocess, "Popen", lambda *args, **kwargs: popen_calls.append((args, kwargs)))

    with pytest.raises(ValueError, match="invalid notification configuration"):
        start.run()

    assert popen_calls == []


def test_run_propagates_system_initialization_error_before_starting_processes(monkeypatch):
    popen_calls = []

    def run_system_initialization(environment):
        raise RuntimeError("system initialization failed")

    def start_process(*args, **kwargs):
        popen_calls.append((args, kwargs))

    monkeypatch.setattr(start, "load_start_config", lambda: start.StartConfig(host="127.0.0.1", port=8000, web_workers=1))
    monkeypatch.setattr(start, "run_system_initialization", run_system_initialization)
    monkeypatch.setattr(start.subprocess, "Popen", start_process)

    with pytest.raises(RuntimeError, match="system initialization failed"):
        start.run()

    assert popen_calls == []


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("0.0.0.0", "127.0.0.1"),
        ("192.0.2.1", "192.0.2.1"),
        ("::1", "::1"),
    ],
)
def test_connect_host_converts_wildcard_addresses(host, expected):
    assert start._connect_host(host) == expected


def test_wait_for_web_service_completes_http_request_before_returning(monkeypatch):
    process = ExitedProcess(return_code=None)
    config = start.StartConfig(host="0.0.0.0", port=8001, web_workers=1)
    events = []

    class Response:
        def read(self):
            events.append("read")

    class Connection:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port, timeout))

        def request(self, method, path, *, headers):
            events.append(("request", method, path, headers))

        def getresponse(self):
            events.append("response")
            return Response()

        def close(self):
            events.append("close")

    monkeypatch.setattr(start.http.client, "HTTPConnection", Connection)

    start.wait_for_web_service(process, config)

    assert events == [
        ("connect", "127.0.0.1", 8001, 0.5),
        ("request", "GET", "/", {"Connection": "close"}),
        "response",
        "read",
        "close",
    ]


@pytest.mark.asyncio
async def test_wait_for_notification_service_reports_early_process_exit(monkeypatch, notification_ready_path):
    process = ExitedProcess(return_code=7)
    ping_calls = []

    async def ping(client):
        ping_calls.append(client)
        return SimpleNamespace(pid=process.pid)

    _stub_notification_client(monkeypatch, ping)

    with pytest.raises(RuntimeError, match="code 7"):
        await start._wait_for_notification_service(process, ready_path=notification_ready_path)

    assert ping_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_pid", [4321, 9876])
async def test_wait_for_notification_service_authenticates_after_ready_report_regardless_of_pid(
    monkeypatch,
    notification_ready_path,
    reported_pid,
):
    process = ExitedProcess(return_code=None, pid=4321)
    ping_calls = []

    async def ping(client):
        ping_calls.append(client)
        return SimpleNamespace(pid=reported_pid)

    _stub_notification_client(monkeypatch, ping)

    assert await start._wait_for_notification_service(process, ready_path=notification_ready_path) is None
    assert len(ping_calls) == 1


@pytest.mark.asyncio
async def test_wait_for_notification_service_requires_ready_report_path_before_ping(monkeypatch):
    process = ExitedProcess(return_code=None)
    ping_calls = []

    async def ping(client):
        ping_calls.append(client)
        return SimpleNamespace(pid=9876)

    _stub_notification_client(monkeypatch, ping)

    with pytest.raises(ValueError):
        await start._wait_for_notification_service(process)

    assert ping_calls == []


@pytest.mark.asyncio
async def test_wait_for_notification_service_does_not_ping_before_ready_report(monkeypatch, tmp_path):
    process = ExitedProcess(return_code=None)
    ready_path = tmp_path / "notification-ready"
    ping_started = asyncio.Event()
    ping_calls = []

    async def ping(client):
        ping_calls.append(client)
        ping_started.set()
        return SimpleNamespace(pid=9876)

    _stub_notification_client(monkeypatch, ping)
    wait_task = asyncio.create_task(start._wait_for_notification_service(process, ready_path=ready_path))

    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ping_started.wait(), timeout=0.02)
        assert ping_calls == []

        ready_path.write_text("", encoding="utf-8")
        await asyncio.wait_for(ping_started.wait(), timeout=1)
        assert await asyncio.wait_for(wait_task, timeout=1) is None
        assert len(ping_calls) == 1
    finally:
        if not wait_task.done():
            wait_task.cancel()
            try:
                await wait_task
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_wait_for_notification_service_rejects_exit_after_ping(monkeypatch, notification_ready_path):
    process = ExitedProcess(return_code=None, pid=4321)

    async def ping(client):
        process.return_code = 9
        return SimpleNamespace(pid=process.pid)

    _stub_notification_client(monkeypatch, ping)

    with pytest.raises(RuntimeError, match="code 9"):
        await start._wait_for_notification_service(process, ready_path=notification_ready_path)


@pytest.mark.asyncio
async def test_wait_for_notification_service_times_out_without_ready_report(monkeypatch, tmp_path):
    process = ExitedProcess(return_code=None)
    ready_path = tmp_path / "notification-ready"
    ping_calls = []

    async def ping(client):
        ping_calls.append(client)
        return SimpleNamespace(pid=process.pid)

    _stub_notification_client(monkeypatch, ping)
    monkeypatch.setattr(start, "NOTIFICATION_START_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(TimeoutError, match="within 0.01 seconds"):
        await start._wait_for_notification_service(process, ready_path=ready_path)

    assert ping_calls == []


@pytest.mark.asyncio
async def test_wait_for_notification_service_has_a_total_timeout(monkeypatch, notification_ready_path):
    process = ExitedProcess(return_code=None)
    ping_calls = []

    async def ping(client):
        ping_calls.append(client)
        await asyncio.Event().wait()

    async def skip_retry_delay(delay):
        return None

    _stub_notification_client(monkeypatch, ping)
    monkeypatch.setattr(start, "NOTIFICATION_START_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(start.asyncio, "sleep", skip_retry_delay)

    with pytest.raises(TimeoutError, match="within 0.01 seconds"):
        await start._wait_for_notification_service(process, ready_path=notification_ready_path)

    assert len(ping_calls) == 1


@pytest.mark.asyncio
async def test_wait_for_remote_notification_service_accepts_any_pid_without_local_process(monkeypatch):
    from app.core.notifications.config import NotificationConfig

    notification_config = NotificationConfig(
        host="127.0.0.1",
        port=8766,
        token=secrets.token_hex(32),
        server_host="192.0.2.20",
    )
    ping_calls = []

    async def ping(client):
        ping_calls.append(client)
        return SimpleNamespace(pid=9876)

    monkeypatch.setattr(start.NotificationClient, "ping", ping)

    assert await start._wait_for_notification_service(None, notification_config) is None
    assert len(ping_calls) == 1


@pytest.mark.asyncio
async def test_wait_for_remote_notification_service_timeout_shows_host_without_token(monkeypatch):
    from app.core.notifications.config import NotificationConfig

    token = secrets.token_hex(32)
    notification_config = NotificationConfig(
        host="127.0.0.1",
        port=8766,
        token=token,
        server_host="192.0.2.20",
    )
    ping_calls = []

    async def ping(client):
        ping_calls.append(client)
        await asyncio.Event().wait()

    async def skip_retry_delay(delay):
        return None

    monkeypatch.setattr(start.NotificationClient, "ping", ping)
    monkeypatch.setattr(start, "NOTIFICATION_START_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(start.asyncio, "sleep", skip_retry_delay)

    with pytest.raises(TimeoutError) as exc_info:
        await start._wait_for_notification_service(None, notification_config)

    error = str(exc_info.value)
    assert "192.0.2.20:8766" in error
    assert token not in error
    assert len(ping_calls) == 1


def test_wait_for_web_service_reports_early_process_exit():
    process = ExitedProcess(return_code=7)
    config = start.StartConfig(host="127.0.0.1", port=8001, web_workers=1)

    with pytest.raises(RuntimeError, match="code 7"):
        start.wait_for_web_service(process, config)


def test_subprocess_options_match_current_platform():
    options = start._subprocess_options()

    if start.os.name == "nt":
        assert options == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert options == {"start_new_session": True}


def test_kill_running_processes_sends_sigkill_to_posix_process_group(monkeypatch):
    process = ExitedProcess(return_code=None)
    process.pid = 1234
    kill_calls = []

    sigkill = object()
    monkeypatch.setattr(start.os, "name", "posix")
    monkeypatch.setattr(start.os, "killpg", lambda pid, shutdown_signal: kill_calls.append((pid, shutdown_signal)), raising=False)
    monkeypatch.setattr(start.signal, "SIGKILL", sigkill, raising=False)

    start._kill_running_processes([process])

    assert kill_calls == [(1234, sigkill)]


def test_stop_processes_force_kills_after_graceful_timeout(monkeypatch):
    process = ExitedProcess(return_code=None)
    requested = []
    killed = []
    wait_results = iter([False, True])

    monkeypatch.setattr(start, "_request_process_stop", requested.append)
    monkeypatch.setattr(start, "_kill_running_processes", lambda processes: killed.append(processes))
    monkeypatch.setattr(start, "_wait_until_stopped", lambda processes, timeout: next(wait_results))

    start.stop_processes([process])

    assert requested == [process]
    assert killed == [[process]]


def test_stop_processes_force_kills_when_graceful_wait_is_interrupted(monkeypatch):
    process = ExitedProcess(return_code=None)
    killed = []
    wait_calls = 0

    def wait_until_stopped(processes, timeout):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            raise KeyboardInterrupt
        return True

    monkeypatch.setattr(start, "_request_process_stop", lambda process: None)
    monkeypatch.setattr(start, "_kill_running_processes", lambda processes: killed.append(processes))
    monkeypatch.setattr(start, "_wait_until_stopped", wait_until_stopped)

    start.stop_processes([process])

    assert killed == [[process]]
    assert wait_calls == 2


def test_stop_processes_swallows_repeated_shutdown_interrupts(monkeypatch):
    process = ExitedProcess(return_code=None)
    killed = []

    monkeypatch.setattr(start, "_request_process_stop", lambda process: None)
    monkeypatch.setattr(start, "_kill_running_processes", lambda processes: killed.append(processes))
    monkeypatch.setattr(
        start,
        "_wait_until_stopped",
        lambda processes, timeout: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    start.stop_processes([process])

    assert killed == [[process], [process]]


def test_install_shutdown_signal_handlers_registers_sigint_and_sigterm(monkeypatch):
    previous_handlers = {
        start.signal.SIGINT: object(),
        start.signal.SIGTERM: object(),
    }
    registered = []

    def install_signal(shutdown_signal, handler):
        registered.append((shutdown_signal, handler))
        return previous_handlers[shutdown_signal]

    monkeypatch.setattr(start.signal, "signal", install_signal)

    installed = start._install_shutdown_signal_handlers()

    assert registered == [
        (start.signal.SIGINT, start._raise_shutdown_interrupt),
        (start.signal.SIGTERM, start._raise_shutdown_interrupt),
    ]
    assert installed == previous_handlers


def test_run_stops_children_and_restores_handlers_after_shutdown_signal(monkeypatch):
    notification_process = ExitedProcess(return_code=None)
    web_process = ExitedProcess(return_code=None)
    processes = iter([notification_process, web_process])
    stopped_processes = []
    restored_handlers = []
    reported_access_urls = []
    previous_handlers = {start.signal.SIGTERM: object()}

    monkeypatch.setattr(start, "load_start_config", lambda: start.StartConfig(host="127.0.0.1", port=8000, web_workers=1))
    monkeypatch.setattr(start, "run_system_initialization", lambda environment: None)
    monkeypatch.setattr(start.subprocess, "Popen", lambda *args, **kwargs: next(processes))
    monkeypatch.setattr(start, "_install_shutdown_signal_handlers", lambda: previous_handlers)
    monkeypatch.setattr(start, "_restore_signal_handlers", restored_handlers.append)
    monkeypatch.setattr(start, "stop_processes", lambda processes: stopped_processes.append(list(processes)))
    monkeypatch.setattr(start, "wait_for_notification_service", lambda process, notification_config, *, ready_path=None: None)
    monkeypatch.setattr(start, "wait_for_web_service", lambda process, config: (_ for _ in ()).throw(KeyboardInterrupt))
    monkeypatch.setattr(start, "report_access_url", reported_access_urls.append)

    return_code = start.run()

    assert return_code == 0
    assert stopped_processes == [[notification_process, web_process]]
    assert restored_handlers == [previous_handlers]
    assert reported_access_urls == []
