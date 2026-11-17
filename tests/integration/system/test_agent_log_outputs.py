import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import psutil
import pytest
from loguru import logger

from app.core.paths import ROOT_DIR
from app.tasks import _delete_log_files, _list_old_log_files


def test_agent_log_identity_is_consistent_across_outputs(tmp_path: Path) -> None:
    repository_root = ROOT_DIR
    database_path = tmp_path / "agent-logs.sqlite3"
    script = textwrap.dedent(
        """
        import asyncio
        import logging
        import os
        from pathlib import Path

        async def main():
            database_path = Path.cwd() / "agent-logs.sqlite3"
            os.environ.pop("PYTEST_CURRENT_TEST", None)
            os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path.as_posix()}"

            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
            from sqlmodel import SQLModel

            from app.core.log import LogManager, agent_log_context, get_logger
            from app.models.system_log import SystemLog
            from app.models.system_setting import SystemSetting
            import app.providers.database as database
            from loguru import logger

            for name in ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool", "aiosqlite"):
                logging.getLogger(name).setLevel(logging.WARNING)

            engine = create_async_engine(os.environ["DATABASE_URL"], connect_args={"timeout": 30})
            async with engine.begin() as connection:
                await connection.run_sync(
                    lambda sync_connection: SQLModel.metadata.create_all(
                        sync_connection,
                        tables=[SystemLog.__table__, SystemSetting.__table__],
                    )
                )
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            database.AsyncSessionLocal = session_factory

            LogManager.setup(log_path=str(Path.cwd() / "agent.log"), level="DEBUG")
            agent_logger = get_logger("agent-output-check")
            with agent_log_context(None, uid="u", session_id="s"):
                agent_logger.info("main-marker")

            with agent_log_context("资料核查", uid="u", session_id="s"):
                agent_logger.info("subagent-marker")
                LogManager.log_tool_call(1, "tool-output-check", "call-marker", "s", "u")
                LogManager.log_tool_result(1, "result-marker", "s", "u")

            agent_logger.info("system-marker")

            current_task = asyncio.current_task()
            sink_tasks = [task for task in asyncio.all_tasks() if task is not current_task]
            for task in sink_tasks:
                await asyncio.gather(task)
            await logger.complete()
            await engine.dispose()

        asyncio.run(main())
        """
    )
    environment = os.environ.copy()
    python_path = [str(repository_root)]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    environment["PYTHONIOENCODING"] = "utf-8"

    with subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as process:
        console, stderr = process.communicate(timeout=30)

    assert process.returncode == 0, stderr

    main_label = "主代理"
    subagent_label = "子代理：资料核查"
    agent_messages = (
        ("main-marker", main_label),
        ("subagent-marker", subagent_label),
        ("call-marker", subagent_label),
        ("result-marker", subagent_label),
    )
    ordinary_logs = list(tmp_path.glob("agent.*.log"))
    tool_logs = list(tmp_path.glob("tools.*.log"))
    assert len(ordinary_logs) == 1
    assert len(tool_logs) == 1
    ordinary_log = ordinary_logs[0].read_text(encoding="utf-8")
    tool_log = tool_logs[0].read_text(encoding="utf-8")

    def assert_prefixed_line(output: str, marker: str, label: str) -> None:
        matching_lines = [line for line in output.splitlines() if marker in line]
        assert len(matching_lines) == 1
        assert f"[{label}] " in matching_lines[0].split(marker, 1)[0]

    for output in (console, ordinary_log):
        for marker, label in agent_messages:
            assert_prefixed_line(output, marker, label)
        system_line = next(line for line in output.splitlines() if "system-marker" in line)
        assert "[主代理] " not in system_line
        assert f"[{subagent_label}] " not in system_line

    for marker, _label in agent_messages[2:]:
        assert_prefixed_line(tool_log, marker, subagent_label)

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT message, uid, session_id, extra FROM system_log ORDER BY id").fetchall()

    records = {marker: next(row for row in rows if marker in row[0]) for marker, _label in (*agent_messages, ("system-marker", ""))}
    assert len(rows) == len(records)

    for marker, label in agent_messages:
        message, uid, session_id, extra_json = records[marker]
        extra = json.loads(extra_json)
        assert message.startswith(f"[{label}] ")
        assert extra["agent_role"] == ("main" if marker == "main-marker" else "subagent")
        assert extra["agent_label"] == label
        assert extra["subagent_name"] == (None if marker == "main-marker" else "资料核查")
        assert (uid, session_id) == ("u", "s")

    system_message, _uid, _session_id, system_extra_json = records["system-marker"]
    system_extra = json.loads(system_extra_json) if system_extra_json else {}
    assert system_message == "system-marker"
    assert not {"agent_role", "agent_label", "subagent_name"}.intersection(system_extra)


def test_log_rotation_preserves_old_files_and_writes_new_archives(tmp_path: Path) -> None:
    repository_root = ROOT_DIR
    script = textwrap.dedent(
        """
        import asyncio
        import io
        import os
        import sys
        import time
        import zipfile
        from datetime import datetime
        from pathlib import Path

        from loguru import logger

        from app.core.log import LogManager, build_process_log_path

        log_path = Path.cwd() / "rotation.log"
        service_path = Path(build_process_log_path(str(log_path)))
        tool_path = Path(build_process_log_path(str(Path.cwd() / "tools.log")))
        old_time = time.time() - 10 * 24 * 60 * 60
        old_stamp = datetime.fromtimestamp(old_time).strftime("%Y-%m-%d_%H-%M-%S_%f")
        old_service_path = service_path.with_name(f"{service_path.stem}.{old_stamp}{service_path.suffix}.zip")
        old_tool_path = tool_path.with_name(f"{tool_path.stem}.{old_stamp}{tool_path.suffix}")

        with zipfile.ZipFile(old_service_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(service_path.name, "old-service-marker")
        old_tool_path.write_text("old-tools-marker", encoding="utf-8")
        os.utime(old_service_path, (old_time, old_time))
        os.utime(old_tool_path, (old_time, old_time))

        console = io.StringIO()
        stdout = sys.stdout
        sys.stdout = console
        try:
            LogManager.setup(log_path=str(log_path), level="CRITICAL")
        finally:
            sys.stdout = stdout

        bound_logger = logger.bind(tool_result=True)
        large_message = "x" * (10_000_000 + 128)
        bound_logger.critical(large_message)
        bound_logger.critical(large_message)
        bound_logger.critical("tail-marker")

        async def drain_logger():
            await logger.complete()

        asyncio.run(drain_logger())
        logger.remove()
        console.close()
        print(os.getpid(), file=stdout)
        """
    )
    environment = os.environ.copy()
    python_path = [str(repository_root)]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    environment["PYTHONIOENCODING"] = "utf-8"

    with subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as process:
        stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 0, stderr

    process_id = int(stdout.strip())
    cutoff = time.time() - 9 * 24 * 60 * 60
    service_archives = list(tmp_path.glob(f"rotation.{process_id}.*.log.zip"))
    old_service_archives = [path for path in service_archives if path.stat().st_mtime <= cutoff]
    assert len(old_service_archives) == 1
    with zipfile.ZipFile(old_service_archives[0]) as archive:
        names = archive.namelist()
        assert names
        assert archive.read(names[0]) == b"old-service-marker"

    new_service_archives = [path for path in service_archives if path.stat().st_mtime > cutoff]
    assert new_service_archives
    new_service_contents = []
    for archive_path in new_service_archives:
        with zipfile.ZipFile(archive_path) as archive:
            for name in archive.namelist():
                new_service_contents.append(archive.read(name))
    assert any(b"x" * 1024 in content for content in new_service_contents)

    tool_rotations = list(tmp_path.glob(f"tools.{process_id}.*.log"))
    old_tool_rotations = [path for path in tool_rotations if path.stat().st_mtime <= cutoff]
    assert len(old_tool_rotations) == 1
    assert old_tool_rotations[0].read_text(encoding="utf-8") == "old-tools-marker"

    new_tool_rotations = [path for path in tool_rotations if path.stat().st_mtime > cutoff]
    assert new_tool_rotations
    assert any(b"x" * 1024 in path.read_bytes() for path in new_tool_rotations)

    service_active = tmp_path / f"rotation.{process_id}.log"
    tool_active = tmp_path / f"tools.{process_id}.log"
    assert b"tail-marker" in service_active.read_bytes()
    assert b"tail-marker" in tool_active.read_bytes()


@pytest.mark.parametrize("exit_mode", ("normal", "abrupt"))
def test_log_cleanup_skips_open_files_then_deletes_after_exit(tmp_path: Path, exit_mode: str) -> None:
    repository_root = ROOT_DIR
    log_path = tmp_path / "restart.log"
    script = textwrap.dedent(
        """
        import asyncio
        import io
        import json
        import os
        import sys
        from pathlib import Path

        from loguru import logger

        from app.core.log import LogManager, build_process_log_path

        log_path = Path(os.environ["LOG_FILE_PATH"])
        service_path = Path(build_process_log_path(str(log_path)))
        tools_path = Path(build_process_log_path(str(log_path.parent / "tools.log")))
        console = io.StringIO()
        stdout = sys.stdout
        sys.stdout = console
        try:
            LogManager.setup(log_path=str(log_path), level="INFO")
        finally:
            sys.stdout = stdout

        bound_logger = logger.bind(tool_result=True)

        async def drain_logger():
            await logger.complete()

        bound_logger.info("before-cleanup-marker")
        asyncio.run(drain_logger())
        print(json.dumps({"service_path": str(service_path), "tools_path": str(tools_path)}), file=stdout, flush=True)

        command = sys.stdin.readline().strip()
        if command == "abrupt":
            os._exit(0)
        if command != "normal":
            raise RuntimeError(command)

        bound_logger.info("after-cleanup-marker")
        asyncio.run(drain_logger())
        logger.remove()
        console.close()
        """
    )
    environment = os.environ.copy()
    python_path = [str(repository_root)]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["LOG_FILE_PATH"] = str(log_path)

    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert process.stdout is not None
        paths = json.loads(process.stdout.readline())
        service_path = Path(paths["service_path"])
        tools_path = Path(paths["tools_path"])

        assert service_path.parent == tmp_path
        assert tools_path.parent == tmp_path
        assert service_path.is_file()
        assert tools_path.is_file()

        old_time = time.time() - 8 * 24 * 60 * 60
        cutoff = time.time() - 7 * 24 * 60 * 60
        os.utime(service_path, (old_time, old_time))
        os.utime(tools_path, (old_time, old_time))

        open_candidates = _list_old_log_files(log_path, cutoff=cutoff, batch_size=10)
        assert set(open_candidates) == {service_path, tools_path}
        warning_messages: list[str] = []
        warning_sink = logger.add(lambda message: warning_messages.append(str(message)), level="WARNING")
        try:
            assert _delete_log_files(open_candidates, cutoff=cutoff) == 0
        finally:
            logger.remove(warning_sink)
        assert not warning_messages
        assert b"before-cleanup-marker" in service_path.read_bytes()
        assert b"before-cleanup-marker" in tools_path.read_bytes()

        assert process.stdin is not None
        stdout, stderr = process.communicate(input=f"{exit_mode}\n", timeout=30)
        assert process.returncode == 0, stderr
        assert not stdout

        os.utime(service_path, (old_time, old_time))
        os.utime(tools_path, (old_time, old_time))
        service_contents = service_path.read_bytes()
        tools_contents = tools_path.read_bytes()
        assert b"before-cleanup-marker" in service_contents
        assert b"before-cleanup-marker" in tools_contents
        if exit_mode == "normal":
            assert b"after-cleanup-marker" in service_contents
            assert b"after-cleanup-marker" in tools_contents

        closed_candidates = _list_old_log_files(log_path, cutoff=cutoff, batch_size=10)
        assert set(closed_candidates) == {service_path, tools_path}
        assert _delete_log_files(closed_candidates, cutoff=cutoff) == 2
        assert not service_path.exists()
        assert not tools_path.exists()
        assert not list(tmp_path.glob("*.zip"))
        assert _list_old_log_files(log_path, cutoff=cutoff, batch_size=10) == []
        assert _delete_log_files([], cutoff=cutoff) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _kill_log_rotation_process_tree(process: subprocess.Popen[str]) -> None:
    # Windows 虚拟环境启动器与实际解释器可能是不同进程，先结束实际子进程。
    try:
        child_processes = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        child_processes = []

    for child_process in reversed(child_processes):
        with suppress(psutil.NoSuchProcess):
            child_process.kill()

    process.kill()
    process.wait(timeout=5)


@pytest.mark.parametrize("log_kind", ("service", "tools"))
def test_log_rotation_interleaved_with_cleanup_preserves_records(tmp_path: Path, log_kind: str) -> None:
    repository_root = ROOT_DIR
    log_path = tmp_path / "rotation.log"
    script = textwrap.dedent(
        """
        import io
        import os
        import sys
        from pathlib import Path
        from unittest.mock import patch

        from loguru import logger

        import app.core.log as log_module
        from app.core.log import LogManager, build_process_log_path

        log_kind = sys.argv[1]
        log_module.LOG_FILE_ROTATION_SIZE = "1 KB"
        log_path = Path.cwd() / "rotation.log"
        selected_log_path = log_path if log_kind == "service" else log_path.with_name("tools.log")
        selected_path = Path(build_process_log_path(str(selected_log_path)))

        console = io.StringIO()
        stdout = sys.stdout
        sys.stdout = console
        try:
            LogManager.setup(log_path=str(log_path), level="CRITICAL")
        finally:
            sys.stdout = stdout

        if log_kind == "service":
            write_log = logger.critical
        else:
            write_log = logger.bind(tool_result=True).debug

        payload = "x" * 650
        before_message = f"before-rotation-marker {payload}"
        trigger_message = f"rotation-trigger-marker {payload}"
        write_log(before_message)
        logger.complete()

        original_rename = os.rename

        def _gated_rename(source, destination, *args, **kwargs):
            if Path(source) == selected_path:
                print(f"rotation-paused {selected_path.name}", file=stdout, flush=True)
                if sys.stdin.readline().strip() != "continue":
                    raise RuntimeError("rotation was not continued")
            return original_rename(source, destination, *args, **kwargs)

        with patch.object(os, "rename", _gated_rename):
            write_log(trigger_message)
            logger.complete()

        write_log("after-rotation-marker")
        logger.complete()
        logger.remove()
        console.close()
        """
    )
    environment = os.environ.copy()
    python_path = [str(repository_root)]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    environment["PYTHONIOENCODING"] = "utf-8"

    with (
        subprocess.Popen(
            [sys.executable, "-c", script, log_kind],
            cwd=tmp_path,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        ) as process,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        try:
            assert process.stdout is not None
            ready_signal = executor.submit(process.stdout.readline).result(timeout=30).strip()
            signal, separator, basename = ready_signal.partition(" ")
            assert signal == "rotation-paused"
            assert separator and basename

            active_path = tmp_path / basename
            assert active_path.parent == tmp_path
            assert active_path.is_file()

            now = time.time()
            old_time = now - 8 * 24 * 60 * 60
            cutoff = now - 7 * 24 * 60 * 60
            os.utime(active_path, (old_time, old_time))

            old_candidates = _list_old_log_files(log_path, cutoff=cutoff, batch_size=10)
            assert old_candidates == [active_path]
            deletion = executor.submit(_delete_log_files, old_candidates, cutoff=cutoff)
            assert deletion.result(timeout=10) == 0
            before_message = b"before-rotation-marker " + b"x" * 650
            assert before_message in active_path.read_bytes()

            assert process.stdin is not None
            stdout, stderr = process.communicate(input="continue\n", timeout=30)
            assert process.returncode == 0, stderr
            assert not stdout
            assert not stderr

            if log_kind == "service":
                archive_paths = list(tmp_path.glob(f"{active_path.stem}.*{active_path.suffix}.zip"))
            else:
                archive_paths = list(tmp_path.glob(f"{active_path.stem}.*{active_path.suffix}"))
            assert len(archive_paths) == 1
            if log_kind == "service":
                with zipfile.ZipFile(archive_paths[0]) as archive:
                    names = archive.namelist()
                    assert names
                    archived_contents = archive.read(names[0])
            else:
                archived_contents = archive_paths[0].read_bytes()
            assert before_message in archived_contents

            active_contents = active_path.read_bytes()
            assert b"rotation-trigger-marker" in active_contents
            assert b"after-rotation-marker" in active_contents
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                _kill_log_rotation_process_tree(process)
