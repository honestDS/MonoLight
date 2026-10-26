import json
import subprocess
import sys
from pathlib import Path

import pytest

SENTINEL = "DATABASE_DIAGNOSTIC_LOGGING_RESULT "
DIAGNOSTIC_MESSAGES = ("sqlite-diagnostic-debug", "sqlite-diagnostic-info")
ORDINARY_MESSAGES = (
    "ordinary-trace",
    "ordinary-debug",
    "ordinary-info",
    "ordinary-warning",
)
DB_MESSAGES = {
    "ordinary-debug",
    "ordinary-info",
    "ordinary-warning",
    "ordinary-tool",
}
VISIBLE_MESSAGES = {
    "INFO": {"ordinary-info", "ordinary-warning", "ordinary-tool"},
    "WARNING": {"ordinary-warning"},
    "TRACE": {*ORDINARY_MESSAGES, "ordinary-tool"},
}

CHILD_SCRIPT = r"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.providers.database as database
from app.core.log import LogManager
from app.models.system_log import SystemLog
from app.models.system_setting import SystemSetting


DIAGNOSTIC_MESSAGES = ("sqlite-diagnostic-debug", "sqlite-diagnostic-info")
ORDINARY_MESSAGES = (
    "ordinary-trace",
    "ordinary-debug",
    "ordinary-info",
    "ordinary-warning",
)


async def main() -> None:
    tmp_dir = Path(sys.argv[1])
    level = sys.argv[2]
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    database_path = tmp_dir / "system-log.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    database.AsyncSessionLocal = session_factory

    def create_tables(sync_connection) -> None:
        SystemSetting.__table__.create(sync_connection, checkfirst=True)
        SystemLog.__table__.create(sync_connection, checkfirst=True)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(create_tables)
        async with session_factory() as session:
            session.add(SystemSetting(key="log_redaction_enabled", value="false"))
            await session.commit()

        LogManager.setup(tmp_dir / "monolight.log", level=level)

        diagnostic_tasks_before = asyncio.all_tasks()
        logger.bind(sqlite_diagnostic=True, tool_call=True).debug(DIAGNOSTIC_MESSAGES[0])
        logger.bind(sqlite_diagnostic=True, tool_call=True).info(DIAGNOSTIC_MESSAGES[1])
        diagnostic_tasks_after = asyncio.all_tasks()
        diagnostic_task_delta = diagnostic_tasks_after - diagnostic_tasks_before
        assert not diagnostic_task_delta

        database_tasks_before = asyncio.all_tasks()
        logger.trace(ORDINARY_MESSAGES[0])
        logger.debug(ORDINARY_MESSAGES[1])
        logger.info(ORDINARY_MESSAGES[2])
        logger.warning(ORDINARY_MESSAGES[3])
        logger.bind(tool_call=True).info("ordinary-tool")
        database_tasks = asyncio.all_tasks() - database_tasks_before
        assert database_tasks
        await asyncio.gather(*database_tasks)
        await logger.complete()

        async with session_factory() as session:
            result = await session.execute(select(SystemLog).order_by(SystemLog.id))
            rows = [
                {
                    "id": row.id,
                    "level": row.level,
                    "module": row.module,
                    "message": row.message,
                    "extra": row.extra,
                }
                for row in result.scalars().all()
            ]

        print(
            "DATABASE_DIAGNOSTIC_LOGGING_RESULT "
            + json.dumps(
                {
                    "pid": os.getpid(),
                    "diagnostic_task_count": len(diagnostic_task_delta),
                    "database_task_count": len(database_tasks),
                    "rows": rows,
                }
            )
        )
    finally:
        await logger.complete()
        logger.remove()
        await engine.dispose()


asyncio.run(main())
"""


@pytest.mark.parametrize("level", ("INFO", "WARNING", "TRACE"))
def test_sqlite_diagnostic_logs_are_routed_to_the_process_file_only(tmp_path: Path, level: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", CHILD_SCRIPT, str(tmp_path), level],
        cwd=".",
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )

    sentinel_lines = [line for line in result.stdout.splitlines() if line.startswith(SENTINEL)]
    assert len(sentinel_lines) == 1
    child_result = json.loads(sentinel_lines[0][len(SENTINEL) :])
    assert isinstance(child_result["pid"], int) and child_result["pid"] > 0
    assert child_result["diagnostic_task_count"] == 0
    assert child_result["database_task_count"] > 0

    main_log_path = tmp_path / f"monolight.{child_result['pid']}.log"
    tools_log_path = tmp_path / f"tools.{child_result['pid']}.log"
    assert main_log_path.is_file()
    assert tools_log_path.is_file()
    main_log = main_log_path.read_text(encoding="utf-8")
    tools_log = tools_log_path.read_text(encoding="utf-8")
    console_output = "\n".join(line for line in result.stdout.splitlines() if not line.startswith(SENTINEL))
    database_messages = {row["message"] for row in child_result["rows"]}

    for message in DIAGNOSTIC_MESSAGES:
        assert message in main_log
        assert message not in tools_log
        assert message not in console_output
        assert message not in database_messages

    assert database_messages == DB_MESSAGES
    assert "ordinary-tool" in tools_log
    assert all(message not in tools_log for message in ORDINARY_MESSAGES)

    for message in (*ORDINARY_MESSAGES, "ordinary-tool"):
        expected = message in VISIBLE_MESSAGES[level]
        assert (message in main_log) is expected
        assert (message in console_output) is expected
