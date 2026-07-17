import subprocess
import sys

import pytest

import start
from app.core.audit.startup import AuditStartupRecoveryResult
from app.core.audit.storage import AuditCleanupResult
from app.models.system_setting import SystemRuntimeSettings


class ExitedProcess:
    def __init__(self, return_code: int | None, pid: int = 1234) -> None:
        self.return_code = return_code
        self.pid = pid

    def poll(self) -> int | None:
        return self.return_code


def test_system_runtime_settings_default_audit_retention_is_ninety_days():
    assert SystemRuntimeSettings().audit_retention_days == 90


def test_system_runtime_settings_reserves_audit_report_email():
    settings = SystemRuntimeSettings(audit_report_email="  audit@example.com  ").normalized()

    assert settings.audit_report_email == "audit@example.com"


@pytest.mark.asyncio
async def test_initialize_system_runs_audit_cleanup_once(monkeypatch):
    from app.core.audit import startup as audit_startup_module
    from app.core.crud import system_setting as system_setting_module
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

    async def get_runtime_settings(received_session):
        events.append(("settings", received_session))
        return SystemRuntimeSettings(audit_retention_days=45)

    async def recover_and_cleanup_audit_data(received_session, *, retention_days):
        events.append(("cleanup", received_session, retention_days))
        return AuditStartupRecoveryResult(
            expired_pending_records=0,
            recovered_preparing_records=0,
            unknown_execution_records=0,
            unknown_execution_attempts=0,
            deleted_database_records=0,
            file_cleanup=AuditCleanupResult(),
        )

    monkeypatch.setattr(database_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(bootstrap_module, "init_system_data", init_system_data)
    monkeypatch.setattr(system_setting_module.system_setting_crud, "get_runtime_settings", get_runtime_settings)
    monkeypatch.setattr(audit_startup_module, "recover_and_cleanup_audit_data", recover_and_cleanup_audit_data)

    await start.initialize_system()

    assert events == [("initialize", session), ("settings", session), ("cleanup", session, 45)]


def test_load_start_config_reads_worker_count_from_environment(monkeypatch):
    monkeypatch.setattr(start, "load_dotenv", lambda: None)
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    monkeypatch.setenv("APP_PORT", "9000")
    monkeypatch.setenv("APP_WORKERS", "3")

    config = start.load_start_config()

    assert config == start.StartConfig(host="127.0.0.1", port=9000, web_workers=3)


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
    ]


def test_build_message_platform_command_starts_exactly_one_worker_process():
    assert start.build_message_platform_command() == [
        sys.executable,
        "-m",
        "app.workers.message_platform",
    ]


def test_build_background_task_command_starts_exactly_one_worker_process():
    assert start.build_background_task_command() == [
        sys.executable,
        "-m",
        "app.workers.background_task",
    ]


def test_build_session_reply_command_starts_exactly_one_worker_process():
    assert start.build_session_reply_command() == [
        sys.executable,
        "-m",
        "app.workers.session_reply",
    ]


def test_report_process_started_includes_name_and_pid(capsys):
    process = ExitedProcess(return_code=None, pid=4321)

    start.report_process_started("Background task worker", process)

    assert capsys.readouterr().out == "Background task worker process started [PID 4321]\n"


def test_run_initializes_system_before_starting_processes(monkeypatch):
    events = []
    process = ExitedProcess(return_code=1)

    def run_coroutine(coroutine):
        events.append("initialize")
        coroutine.close()

    def start_process(*args, **kwargs):
        events.append("process")
        return process

    monkeypatch.setattr(start, "load_start_config", lambda: start.StartConfig(host="127.0.0.1", port=8000, web_workers=2))
    monkeypatch.setattr(start.asyncio, "run", run_coroutine)
    monkeypatch.setattr(start.subprocess, "Popen", start_process)
    monkeypatch.setattr(start, "wait_for_web_service", lambda process, config: None)
    monkeypatch.setattr(start, "stop_processes", lambda processes: None)

    return_code = start.run()

    assert return_code == 1
    assert events == ["initialize", "process", "process", "process", "process"]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("0.0.0.0", "127.0.0.1"),
        ("::", "127.0.0.1"),
        ("", "127.0.0.1"),
        ("192.0.2.1", "192.0.2.1"),
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
    process = ExitedProcess(return_code=None)
    stopped_processes = []
    restored_handlers = []
    previous_handlers = {start.signal.SIGTERM: object()}

    def run_coroutine(coroutine):
        coroutine.close()

    monkeypatch.setattr(start, "load_start_config", lambda: start.StartConfig(host="127.0.0.1", port=8000, web_workers=1))
    monkeypatch.setattr(start.asyncio, "run", run_coroutine)
    monkeypatch.setattr(start.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(start, "_install_shutdown_signal_handlers", lambda: previous_handlers)
    monkeypatch.setattr(start, "_restore_signal_handlers", restored_handlers.append)
    monkeypatch.setattr(start, "stop_processes", lambda processes: stopped_processes.append(list(processes)))
    monkeypatch.setattr(start, "wait_for_web_service", lambda process, config: (_ for _ in ()).throw(KeyboardInterrupt))

    return_code = start.run()

    assert return_code == 0
    assert stopped_processes == [[process]]
    assert restored_handlers == [previous_handlers]
