from __future__ import annotations

import asyncio
import ctypes
import math
import os
import platform
import signal
import time
import uuid
from dataclasses import dataclass
from typing import Any

import psutil

from app.core.constants import TERMINAL_WINDOWS_BOOT_TIME_TOLERANCE_SECONDS

__all__ = [
    "TerminalProcessCleanupResult",
    "capture_terminal_process_identity",
    "cleanup_terminal_process_identity",
]

MAX_KNOWN_PROCESSES = 256
PROCESS_WAIT_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class TerminalProcessCleanupResult:
    matched_processes: tuple[int, ...]
    terminated_processes: tuple[int, ...]
    errors: tuple[str, ...]

    @property
    def succeeded(self) -> bool:
        return not self.errors


def _is_valid_pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_valid_create_time(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _same_create_time(actual: Any, expected: float) -> bool:
    return _is_valid_create_time(actual) and float(actual) == expected


def _read_windows_boot_id() -> str | None:
    if platform.system() != "Windows":
        return None
    try:
        ntdll = ctypes.WinDLL("ntdll")
        query_system_information = ntdll.NtQuerySystemInformation
        query_system_information.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        query_system_information.restype = ctypes.c_long
        buffer = ctypes.create_string_buffer(64)
        returned_length = ctypes.c_ulong()
        status = query_system_information(
            90,
            buffer,
            ctypes.sizeof(buffer),
            ctypes.byref(returned_length),
        )
        if status != 0 or returned_length.value < 16:
            return None
        return str(uuid.UUID(bytes_le=buffer.raw[:16]))
    except Exception:
        return None


def _normalise_identity(identity: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(identity, dict):
        return None

    root_pid = identity.get("root_pid")
    root_create_time = identity.get("root_create_time")
    identity_platform = identity.get("platform")
    boot_time = identity.get("boot_time")
    boot_id = identity.get("boot_id")
    known_processes = identity.get("known_processes")
    if not _is_valid_pid(root_pid) or not _is_valid_create_time(root_create_time) or not isinstance(identity_platform, str) or not identity_platform or not _is_valid_create_time(boot_time) or not isinstance(known_processes, dict):
        return None
    if boot_id is not None:
        if not isinstance(boot_id, str):
            return None
        try:
            boot_id = str(uuid.UUID(boot_id))
        except (ValueError, AttributeError, TypeError):
            return None

    normalised_known_processes: dict[str, float] = {}
    for raw_pid, raw_create_time in known_processes.items():
        if len(normalised_known_processes) >= MAX_KNOWN_PROCESSES:
            break
        if not isinstance(raw_pid, str):
            continue
        try:
            process_pid = int(raw_pid)
        except ValueError:
            continue
        if str(process_pid) != raw_pid or not _is_valid_pid(process_pid) or not _is_valid_create_time(raw_create_time):
            continue
        normalised_known_processes[raw_pid] = float(raw_create_time)

    process_group_id = identity.get("process_group_id")
    if process_group_id is not None and not _is_valid_pid(process_group_id):
        process_group_id = None

    normalised_identity = {
        "platform": identity_platform,
        "boot_time": float(boot_time),
        "root_pid": root_pid,
        "root_create_time": float(root_create_time),
        "process_group_id": process_group_id,
        "known_processes": normalised_known_processes,
    }
    if boot_id is not None:
        normalised_identity["boot_id"] = boot_id
    return normalised_identity


def _compare_startup_identity(
    identity: dict[str, Any],
    current_platform: str,
    current_boot_time: Any,
    current_boot_id: str | None,
) -> tuple[bool, str | None]:
    if identity["platform"] != current_platform:
        return False, None

    stored_boot_id = identity.get("boot_id")
    if stored_boot_id is not None:
        if current_boot_id is None:
            return False, "unable to read Windows boot identifier"
        try:
            normalised_current_boot_id = str(uuid.UUID(current_boot_id))
        except (ValueError, AttributeError, TypeError):
            return False, "unable to read Windows boot identifier"
        return normalised_current_boot_id == stored_boot_id, None

    if not _is_valid_create_time(current_boot_time):
        return False, "unable to read boot time"
    if current_platform == "Windows":
        return (
            math.isclose(
                float(current_boot_time),
                identity["boot_time"],
                rel_tol=0.0,
                abs_tol=TERMINAL_WINDOWS_BOOT_TIME_TOLERANCE_SECONDS,
            ),
            None,
        )
    return _same_create_time(current_boot_time, identity["boot_time"]), None


def _safe_process_create_time(process: psutil.Process) -> float | None:
    try:
        create_time = process.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None
    if not _is_valid_create_time(create_time):
        return None
    return float(create_time)


def _retain_previous_processes(previous_identity: dict[str, Any]) -> dict[str, float]:
    retained: dict[str, float] = {}
    for raw_pid, expected_create_time in previous_identity["known_processes"].items():
        if len(retained) >= MAX_KNOWN_PROCESSES:
            break
        process_pid = int(raw_pid)
        try:
            process = psutil.Process(process_pid)
            actual_create_time = process.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if _same_create_time(actual_create_time, expected_create_time):
            retained[raw_pid] = expected_create_time
    return retained


def _capture_process_identity(pid: int, previous_identity: dict[str, Any] | None) -> dict[str, Any] | None:
    if not _is_valid_pid(pid):
        return None

    current_platform = platform.system()
    current_boot_id = _read_windows_boot_id() if current_platform == "Windows" else None
    try:
        current_boot_time = psutil.boot_time()
    except Exception:
        return None
    if not isinstance(current_platform, str) or not current_platform or not _is_valid_create_time(current_boot_time):
        return None

    previous = _normalise_identity(previous_identity)
    if previous is not None:
        previous_matches, comparison_error = _compare_startup_identity(
            previous,
            current_platform,
            current_boot_time,
            current_boot_id,
        )
        if comparison_error is not None:
            return None
        if not previous_matches:
            previous = None
    previous_processes = _retain_previous_processes(previous) if previous is not None and previous["root_pid"] == pid else {}
    previous_snapshot = None
    if previous is not None:
        previous_snapshot = {
            **previous,
            "known_processes": previous_processes,
        }
        if current_boot_id is not None:
            previous_snapshot["boot_id"] = current_boot_id
    try:
        root_process = psutil.Process(pid)
        root_create_time = _safe_process_create_time(root_process)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        if previous is None or previous["root_pid"] != pid:
            return None
        return previous_snapshot

    if root_create_time is None:
        if previous is not None and previous["root_pid"] == pid:
            return previous_snapshot
        return None
    if (
        previous is not None
        and previous["root_pid"] == pid
        and not _same_create_time(
            root_create_time,
            previous["root_create_time"],
        )
    ):
        return previous_snapshot

    known_processes: dict[str, float] = {str(pid): root_create_time}
    try:
        children = root_process.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        children = ()

    for process in children:
        if len(known_processes) >= MAX_KNOWN_PROCESSES:
            break
        process_pid = getattr(process, "pid", None)
        if not _is_valid_pid(process_pid) or process_pid == pid:
            continue
        process_create_time = _safe_process_create_time(process)
        if process_create_time is not None:
            known_processes[str(process_pid)] = process_create_time

    for raw_pid, expected_create_time in previous_processes.items():
        if len(known_processes) >= MAX_KNOWN_PROCESSES:
            break
        known_processes.setdefault(raw_pid, expected_create_time)

    process_group_id = None
    if current_platform.lower() == "linux":
        try:
            process_group_id = os.getpgid(pid)
        except OSError:
            process_group_id = None

    captured_identity = {
        "platform": current_platform,
        "boot_time": float(current_boot_time),
        "root_pid": pid,
        "root_create_time": root_create_time,
        "process_group_id": process_group_id,
        "known_processes": known_processes,
    }
    if current_boot_id is not None:
        captured_identity["boot_id"] = current_boot_id
    return captured_identity


async def capture_terminal_process_identity(
    pid: int,
    previous_identity: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(_capture_process_identity, pid, previous_identity)
    except Exception:
        return None


def _append_process_error(errors: list[str], operation: str, pid: int, error: BaseException) -> None:
    errors.append(f"{operation} pid {pid}: {error}")


def _process_depth(pid: int, parent_pids: dict[int, int | None]) -> int:
    depth = 0
    current_pid = pid
    visited: set[int] = set()
    while current_pid in parent_pids and current_pid not in visited:
        visited.add(current_pid)
        parent_pid = parent_pids[current_pid]
        if parent_pid is None or parent_pid not in parent_pids:
            break
        depth += 1
        current_pid = parent_pid
    return depth


def _wait_for_processes(
    processes: list[psutil.Process],
    timeout: float,
    errors: list[str],
) -> list[psutil.Process]:
    if not processes:
        return []
    try:
        _, alive = psutil.wait_procs(processes, timeout=max(0.0, timeout))
    except Exception as error:
        errors.append(f"wait for processes: {error}")
        return processes
    return list(alive)


def _converge_terminated_processes(
    signaled_processes: dict[int, psutil.Process],
    errors: list[str],
) -> None:
    if not signaled_processes:
        return

    deadline = time.monotonic() + PROCESS_WAIT_TIMEOUT_SECONDS
    alive = _wait_for_processes(list(signaled_processes.values()), PROCESS_WAIT_TIMEOUT_SECONDS, errors)
    if not alive:
        return

    retry_processes: list[psutil.Process] = []
    for process in alive:
        process_pid = getattr(process, "pid", 0)
        try:
            process.kill()
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, psutil.ZombieProcess) as error:
            _append_process_error(errors, "retry kill", process_pid, error)
            retry_processes.append(process)
        except Exception as error:
            _append_process_error(errors, "retry kill", process_pid, error)
            retry_processes.append(process)
        else:
            retry_processes.append(process)

    if not retry_processes:
        return
    remaining_timeout = max(0.0, deadline - time.monotonic())
    still_alive = _wait_for_processes(retry_processes, remaining_timeout, errors)
    for process in still_alive:
        process_pid = getattr(process, "pid", 0)
        errors.append(f"process did not terminate pid {process_pid}")


def _cleanup_process_identity(identity: dict[str, Any] | None) -> TerminalProcessCleanupResult:
    normalised_identity = _normalise_identity(identity)
    if normalised_identity is None:
        return TerminalProcessCleanupResult((), (), ("invalid process identity",))

    current_platform = platform.system()
    if normalised_identity["platform"] != current_platform:
        return TerminalProcessCleanupResult((), (), ())

    current_boot_time: float | None = None
    current_boot_id: str | None = None
    if normalised_identity.get("boot_id") is not None:
        current_boot_id = _read_windows_boot_id()
    else:
        try:
            current_boot_time = psutil.boot_time()
        except Exception as error:
            return TerminalProcessCleanupResult((), (), (f"unable to read boot time: {error}",))
    startup_matches, comparison_error = _compare_startup_identity(
        normalised_identity,
        current_platform,
        current_boot_time,
        current_boot_id,
    )
    if comparison_error is not None:
        return TerminalProcessCleanupResult((), (), (comparison_error,))
    if not startup_matches:
        return TerminalProcessCleanupResult((), (), ())

    errors: list[str] = []
    current_pid = os.getpid()
    root_pid = normalised_identity["root_pid"]
    if root_pid == current_pid:
        return TerminalProcessCleanupResult((), (), ("refusing to terminate current process",))

    current_process_create_time = normalised_identity["known_processes"].get(str(current_pid))
    if current_process_create_time is not None:
        try:
            actual_current_process_create_time = psutil.Process(current_pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            actual_current_process_create_time = None
        if _same_create_time(actual_current_process_create_time, current_process_create_time):
            return TerminalProcessCleanupResult((), (), ("refusing to terminate current process",))

    expected_create_times: dict[int, float] = {}
    expected_create_times[root_pid] = normalised_identity["root_create_time"]
    for raw_pid, expected_create_time in normalised_identity["known_processes"].items():
        process_pid = int(raw_pid)
        if process_pid == root_pid and expected_create_time != normalised_identity["root_create_time"]:
            errors.append(f"conflicting create time for pid {process_pid}")
            continue
        expected_create_times.setdefault(process_pid, expected_create_time)

    matched_processes: dict[int, psutil.Process] = {}
    parent_pids: dict[int, int | None] = {}
    root_process: psutil.Process | None = None
    for process_pid, expected_create_time in expected_create_times.items():
        if process_pid == current_pid:
            errors.append(f"refusing to terminate current process pid {process_pid}")
            continue
        try:
            process = psutil.Process(process_pid)
            actual_create_time = process.create_time()
            if not _same_create_time(actual_create_time, expected_create_time):
                continue
            matched_processes[process_pid] = process
            try:
                parent_pids[process_pid] = process.ppid()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                parent_pids[process_pid] = None
            if process_pid == root_pid:
                root_process = process
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, psutil.ZombieProcess) as error:
            _append_process_error(errors, "inspect", process_pid, error)
        except Exception as error:
            _append_process_error(errors, "inspect", process_pid, error)

    if root_process is not None:
        try:
            current_children = root_process.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            current_children = ()
        for process in current_children:
            process_pid = getattr(process, "pid", None)
            if not _is_valid_pid(process_pid) or process_pid == current_pid:
                continue
            try:
                actual_create_time = process.create_time()
                expected_create_time = expected_create_times.get(process_pid)
                if expected_create_time is not None and not _same_create_time(actual_create_time, expected_create_time):
                    continue
                matched_processes.setdefault(process_pid, process)
                try:
                    parent_pids[process_pid] = process.ppid()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    parent_pids[process_pid] = None
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess) as error:
                _append_process_error(errors, "inspect", process_pid, error)
            except Exception as error:
                _append_process_error(errors, "inspect", process_pid, error)

    matched_pids = tuple(matched_processes)
    if not matched_processes:
        return TerminalProcessCleanupResult(matched_pids, (), tuple(errors))

    process_order = sorted(
        matched_processes,
        key=lambda process_pid: (-_process_depth(process_pid, parent_pids), process_pid),
    )
    terminated_processes: list[int] = []
    signaled_processes: dict[int, psutil.Process] = {}
    group_terminated: set[int] = set()
    is_linux = current_platform.lower() == "linux"
    recorded_group_id = normalised_identity["process_group_id"]

    if is_linux and recorded_group_id is not None:
        group_members: list[int] = []
        try:
            current_group_id = os.getpgid(current_pid)
        except OSError as error:
            current_group_id = None
            _append_process_error(errors, "inspect process group", current_pid, error)

        if current_group_id == recorded_group_id:
            errors.append(f"refusing to terminate current process group {recorded_group_id}")
        elif current_group_id is not None:
            for process_pid in matched_processes:
                try:
                    if os.getpgid(process_pid) == recorded_group_id:
                        group_members.append(process_pid)
                except ProcessLookupError:
                    continue
                except OSError as error:
                    _append_process_error(errors, "inspect process group", process_pid, error)

            if group_members:
                try:
                    os.killpg(recorded_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except (PermissionError, OSError) as error:
                    _append_process_error(errors, "kill process group", recorded_group_id, error)
                else:
                    group_terminated.update(group_members)
                    terminated_processes.extend(group_members)
                    signaled_processes.update((process_pid, matched_processes[process_pid]) for process_pid in group_members)

    for process_pid in process_order:
        if process_pid in group_terminated:
            continue
        process = matched_processes[process_pid]
        try:
            process.kill()
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, psutil.ZombieProcess) as error:
            _append_process_error(errors, "kill", process_pid, error)
        except Exception as error:
            _append_process_error(errors, "kill", process_pid, error)
        else:
            terminated_processes.append(process_pid)
            signaled_processes[process_pid] = process

    _converge_terminated_processes(signaled_processes, errors)

    return TerminalProcessCleanupResult(matched_pids, tuple(terminated_processes), tuple(errors))


async def cleanup_terminal_process_identity(
    identity: dict[str, Any] | None,
) -> TerminalProcessCleanupResult:
    try:
        return await asyncio.to_thread(_cleanup_process_identity, identity)
    except Exception as error:
        return TerminalProcessCleanupResult((), (), (f"cleanup failed: {error}",))
