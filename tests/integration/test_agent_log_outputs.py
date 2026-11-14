import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path


def test_agent_log_identity_is_consistent_across_outputs(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
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
    repository_root = Path(__file__).resolve().parents[2]
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
