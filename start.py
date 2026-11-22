import asyncio
import http.client
import ipaddress
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from dotenv import load_dotenv

from app.core.constants import (
    ERR_NOTIFICATION_READY_REPORT_REQUIRED,
    NOTIFICATION_IO_TIMEOUT_SECONDS,
    NOTIFICATION_READY_FILE_ENV,
    NOTIFICATION_START_TIMEOUT_SECONDS,
)
from app.core.event_loop import get_uvicorn_loop
from app.core.i18n import t
from app.core.notifications.client import NotificationClient
from app.core.notifications.config import NotificationConfig, load_notification_config
from app.core.notifications.protocol import NotificationProtocolError
from app.core.paths import DASHBOARD_INDEX_PATH
from app.core.system_secrets import initialize_system_secrets

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_WEB_WORKERS = 1
WEB_START_TIMEOUT_SECONDS = 60.0
PROCESS_STOP_TIMEOUT_SECONDS = 3.0
PROCESS_KILL_TIMEOUT_SECONDS = 2.0
PROCESS_POLL_INTERVAL_SECONDS = 0.05
INITIALIZE_SYSTEM_CODE = """\
import asyncio
from start import initialize_system

asyncio.run(initialize_system())
"""


@dataclass(frozen=True)
class StartConfig:
    host: str
    port: int
    web_workers: int


def _parse_positive_int(name: str, default: int, *, maximum: int | None = None) -> int:
    raw_value = (os.getenv(name) or str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1 or (maximum is not None and value > maximum):
        range_text = f" between 1 and {maximum}" if maximum is not None else " greater than or equal to 1"
        raise ValueError(f"{name} must be{range_text}")
    return value


def _parse_host() -> str:
    host = (os.getenv("APP_HOST") or "").strip() or DEFAULT_HOST
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("APP_HOST must be a valid IPv4 or IPv6 address literal") from exc

    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id is not None:
            raise ValueError("APP_HOST must be an unscoped IPv4 or IPv6 address literal")
        if address.is_unspecified:
            raise ValueError("APP_HOST must not be the unspecified IPv6 address")

    return str(address)


def load_start_config() -> StartConfig:
    load_dotenv()
    return StartConfig(
        host=_parse_host(),
        port=_parse_positive_int("APP_PORT", DEFAULT_PORT, maximum=65535),
        web_workers=_parse_positive_int("APP_WORKERS", DEFAULT_WEB_WORKERS),
    )


def validate_dashboard_assets() -> None:
    if not DASHBOARD_INDEX_PATH.is_file():
        raise RuntimeError(f"Pre-built Dashboard assets are missing: {DASHBOARD_INDEX_PATH}")


def _subprocess_options() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def build_web_command(config: StartConfig) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        config.host,
        "--port",
        str(config.port),
        "--workers",
        str(config.web_workers),
        "--loop",
        get_uvicorn_loop(),
    ]


def build_initialize_command() -> list[str]:
    return [sys.executable, "-c", INITIALIZE_SYSTEM_CODE]


def run_system_initialization(child_environment: dict[str, str]) -> None:
    result = subprocess.run(build_initialize_command(), env=child_environment, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"System initialization exited with code {result.returncode}")


def build_message_platform_command() -> list[str]:
    return [sys.executable, "-m", "app.workers.message_platform"]


def build_background_task_command() -> list[str]:
    return [sys.executable, "-m", "app.workers.background_task"]


def build_general_command() -> list[str]:
    return [sys.executable, "-m", "app.workers.general"]


def build_memory_command() -> list[str]:
    return [sys.executable, "-m", "app.workers.memory"]


def build_terminal_command() -> list[str]:
    return [sys.executable, "-m", "app.workers.terminal"]


def build_session_reply_command() -> list[str]:
    return [sys.executable, "-m", "app.workers.session_reply"]


def build_notification_command() -> list[str]:
    return [sys.executable, "-m", "app.workers.notification"]


def report_process_started(process_name: str, process: subprocess.Popen) -> None:
    print(f"{process_name} process started [PID {process.pid}]", flush=True)


async def initialize_system() -> None:
    load_dotenv()
    await asyncio.to_thread(initialize_system_secrets)

    from app.core.audit.startup import recover_audit_state
    from app.providers.database import AsyncSessionLocal
    from app.providers.database.bootstrap import init_system_data

    async with AsyncSessionLocal() as session:
        await init_system_data(session)
        await recover_audit_state(session)


def _connect_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    return host


def build_access_url(config: StartConfig) -> str:
    host = _connect_host(config.host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{config.port}/"


def _should_color_access_url() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def report_access_url(config: StartConfig) -> None:
    message = f"Dashboard access URL: {build_access_url(config)} you can access it in your browser or use."
    if _should_color_access_url():
        message = f"\033[1;92m{message}\033[0m"
    print(message, flush=True)


def wait_for_web_service(process: subprocess.Popen, config: StartConfig) -> None:
    deadline = time.monotonic() + WEB_START_TIMEOUT_SECONDS
    connect_host = _connect_host(config.host)
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"Web service exited during startup with code {return_code}")
        connection = http.client.HTTPConnection(connect_host, config.port, timeout=0.5)
        try:
            connection.request("GET", "/", headers={"Connection": "close"})
            response = connection.getresponse()
            response.read()
            return
        except (OSError, http.client.HTTPException):
            time.sleep(0.25)
        finally:
            connection.close()
    raise TimeoutError(f"Web service did not listen on {connect_host}:{config.port} within {WEB_START_TIMEOUT_SECONDS:g} seconds")


def _raise_if_notification_process_exited(process: subprocess.Popen | None) -> None:
    if process is None:
        return

    return_code = process.poll()
    if return_code is None:
        return

    raise RuntimeError(f"Notification service exited during startup with code {return_code}")


async def _wait_for_notification_service(
    process: subprocess.Popen | None,
    notification_config: NotificationConfig | None = None,
    *,
    ready_path: Path | None = None,
) -> None:
    if process is not None and ready_path is None:
        raise ValueError(t(ERR_NOTIFICATION_READY_REPORT_REQUIRED))

    loop = asyncio.get_running_loop()
    deadline = loop.time() + NOTIFICATION_START_TIMEOUT_SECONDS
    if notification_config is None:
        notification_config = await asyncio.to_thread(load_notification_config)
    client = NotificationClient(notification_config, "launcher")
    listening_reported = process is None

    while True:
        _raise_if_notification_process_exited(process)

        if not listening_reported:
            assert ready_path is not None
            listening_reported = await asyncio.to_thread(ready_path.is_file)

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"Notification service did not become ready on {notification_config.connect_host}:{notification_config.port} within {NOTIFICATION_START_TIMEOUT_SECONDS:g} seconds")

        if not listening_reported:
            await asyncio.sleep(min(0.1, remaining))
            continue

        try:
            await asyncio.wait_for(
                client.ping(),
                timeout=min(NOTIFICATION_IO_TIMEOUT_SECONDS, remaining),
            )
        except (OSError, EOFError, TimeoutError, NotificationProtocolError):
            await asyncio.sleep(0.1)
            continue

        _raise_if_notification_process_exited(process)
        return


def wait_for_notification_service(
    process: subprocess.Popen | None,
    notification_config: NotificationConfig | None = None,
    *,
    ready_path: Path | None = None,
) -> None:
    asyncio.run(_wait_for_notification_service(process, notification_config, ready_path=ready_path))


def _request_process_stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        process.terminate()


def _kill_running_processes(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                process.kill()
            except OSError:
                pass


def _wait_until_stopped(processes: list[subprocess.Popen], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while any(process.poll() is None for process in processes):
        if time.monotonic() >= deadline:
            return False
        time.sleep(PROCESS_POLL_INTERVAL_SECONDS)
    return True


def stop_processes(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        _request_process_stop(process)

    try:
        stopped_gracefully = _wait_until_stopped(processes, PROCESS_STOP_TIMEOUT_SECONDS)
    except KeyboardInterrupt:
        stopped_gracefully = False

    if stopped_gracefully:
        return

    _kill_running_processes(processes)
    try:
        _wait_until_stopped(processes, PROCESS_KILL_TIMEOUT_SECONDS)
    except KeyboardInterrupt:
        _kill_running_processes(processes)


def _raise_shutdown_interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def _install_shutdown_signal_handlers() -> dict[int, signal.Handlers]:
    previous_handlers = {}
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[shutdown_signal] = signal.signal(shutdown_signal, _raise_shutdown_interrupt)
    return previous_handlers


def _restore_signal_handlers(previous_handlers: dict[int, signal.Handlers]) -> None:
    for shutdown_signal, previous_handler in previous_handlers.items():
        signal.signal(shutdown_signal, previous_handler)


def run() -> int:
    config = load_start_config()
    validate_dashboard_assets()
    child_environment = os.environ.copy()
    run_system_initialization(child_environment)
    notification_config = load_notification_config()
    process_options = _subprocess_options()
    processes: list[subprocess.Popen] = []
    previous_handlers = _install_shutdown_signal_handlers()
    ready_directory = None

    try:
        notification_process = None
        if notification_config.server_host is None:
            ready_directory = TemporaryDirectory(prefix="monolight-notification-start-")
            ready_path = Path(ready_directory.name) / "ready"
            notification_environment = child_environment.copy()
            notification_environment[NOTIFICATION_READY_FILE_ENV] = str(ready_path)
            notification_process = subprocess.Popen(build_notification_command(), env=notification_environment, **process_options)
            processes.append(notification_process)
            report_process_started("Notification center", notification_process)
            wait_for_notification_service(notification_process, notification_config, ready_path=ready_path)
        else:
            wait_for_notification_service(None, notification_config)

        web_process = subprocess.Popen(build_web_command(config), env=child_environment, **process_options)
        processes.append(web_process)
        report_process_started("Web", web_process)

        wait_for_web_service(web_process, config)
        general_process = subprocess.Popen(build_general_command(), env=child_environment, **process_options)
        processes.append(general_process)
        report_process_started("General worker", general_process)
        memory_process = subprocess.Popen(build_memory_command(), env=child_environment, **process_options)
        processes.append(memory_process)
        report_process_started("Memory worker", memory_process)
        terminal_process = subprocess.Popen(build_terminal_command(), env=child_environment, **process_options)
        processes.append(terminal_process)
        report_process_started("Terminal worker", terminal_process)
        report_access_url(config)

        while True:
            for process in processes:
                return_code = process.poll()
                if return_code is not None:
                    return return_code if return_code != 0 else 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            stop_processes(processes)
        finally:
            try:
                if ready_directory is not None:
                    ready_directory.cleanup()
            finally:
                _restore_signal_handlers(previous_handlers)


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
