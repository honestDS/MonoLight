import asyncio
import json
import sqlite3
import sys
import threading
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from loguru import logger
from sqlalchemy import Column, Integer, MetaData, String, Table, select, text, update
from sqlalchemy.exc import OperationalError

from app.models.system_setting import SystemSetting
from app.providers.database import (
    AsyncSessionLocal,
    engine,
    ensure_sqlite_outer_transaction,
)
from app.providers.database.client import CancellationSafeAsyncSession
from app.providers.database.diagnostics import install_sqlite_diagnostics


class _DiagnosticCapture:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.changed = asyncio.Event()

    def sink(self, message: Any) -> None:
        raw_message = message.record["message"]
        prefix = "[sqlite-debug] "
        if raw_message.startswith(prefix):
            self.records.append(json.loads(raw_message[len(prefix) :]))
            self.changed.set()


@pytest.fixture
def sqlite_diagnostic_logs() -> _DiagnosticCapture:
    capture = _DiagnosticCapture()
    sink_id = logger.add(
        capture.sink,
        level="DEBUG",
        filter=lambda record: bool(record["extra"].get("sqlite_diagnostic")),
    )
    try:
        yield capture
    finally:
        logger.remove(sink_id)


async def _wait_for_diagnostic(
    capture: _DiagnosticCapture,
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    while True:
        for record in reversed(capture.records):
            if predicate(record):
                return record
        capture.changed.clear()
        await asyncio.wait_for(capture.changed.wait(), timeout=2)


def _new_probe_table(prefix: str = "transaction_probe") -> Table:
    return Table(
        f"{prefix}_{uuid.uuid4().hex}",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("value", String(100), nullable=False),
    )


async def _create_probe_table(table: Table) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: table.create(sync_connection))
        await connection.execute(table.insert().values(id=1, value="initial"))


async def _drop_probe_table(table: Table) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: table.drop(sync_connection, checkfirst=True))


async def _create_system_setting_table() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SystemSetting.__table__.create(sync_connection, checkfirst=True))


async def _drop_system_setting_table() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SystemSetting.__table__.drop(sync_connection, checkfirst=True))


def _clear_diagnostics(capture: _DiagnosticCapture) -> None:
    capture.records.clear()
    capture.changed.clear()


@pytest.fixture(autouse=True)
async def _database_factory(database_factory, monkeypatch):
    async with database_factory(
        create_schema=False,
        foreign_keys=True,
        wal=True,
        session_class=CancellationSafeAsyncSession,
    ) as factory:
        install_sqlite_diagnostics(factory.kw["bind"], slow_transaction_seconds=0.03)
        module = sys.modules[__name__]
        monkeypatch.setattr(module, "engine", factory.kw["bind"])
        monkeypatch.setattr(module, "AsyncSessionLocal", factory)
        yield


@pytest.mark.asyncio
async def test_sqlite_nested_savepoint_is_not_visible_before_outer_commit() -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite transaction semantics only")

    table_name = f"transaction_probe_{uuid.uuid4().hex}"
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY)")

    try:
        async with AsyncSessionLocal() as writer:
            await ensure_sqlite_outer_transaction(writer)
            async with writer.begin_nested():
                await writer.execute(text(f"INSERT INTO {table_name} (id) VALUES (1)"))

            async with AsyncSessionLocal() as reader:
                visible_count = await reader.scalar(text(f"SELECT COUNT(*) FROM {table_name} WHERE id = 1"))

            assert visible_count == 0
            await writer.rollback()
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table_name}")


@pytest.mark.asyncio
async def test_sqlite_diagnostic_reports_real_write_contention(sqlite_diagnostic_logs: _DiagnosticCapture) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite transaction diagnostics only")

    table = _new_probe_table()
    await _create_probe_table(table)
    _clear_diagnostics(sqlite_diagnostic_logs)
    holder_ready = asyncio.Event()
    release_holder = asyncio.Event()
    holder_task: asyncio.Task[None] | None = None
    secret = f"private-{uuid.uuid4().hex}"

    async def hold_write() -> None:
        async with AsyncSessionLocal() as holder:
            await holder.execute(update(table).where(table.c.id == 1).values(value=secret))
            holder_ready.set()
            await release_holder.wait()
            await holder.rollback()

    try:
        holder_task = asyncio.create_task(hold_write())
        await asyncio.wait_for(holder_ready.wait(), timeout=2)
        holder_write = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "write_completed" and record["last_successful_operation"] == {"type": "update", "target": table.name},
        )
        holder_transaction_id = holder_write["transaction_id"]
        holder_long = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "long_transaction" and record["transaction_id"] == holder_transaction_id,
        )
        assert holder_long["driver_in_transaction"] is True
        assert holder_long["owner_task"]["task_id"] == id(holder_task)
        assert any(frame["function"] == "hold_write" for frame in holder_long["owner_task"]["await"])
        assert any(frame["file"] == "tests/integration/test_database_sqlite_transactions.py" for frame in holder_long["source"])

        async with AsyncSessionLocal() as contender:
            await contender.execute(text("PRAGMA busy_timeout=1"))
            with pytest.raises(OperationalError) as error_info:
                await contender.execute(update(table).where(table.c.id == 1).values(value="contender"))
            assert isinstance(error_info.value.orig, sqlite3.OperationalError)
            await contender.rollback()

        lock_error = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "lock_error" and record["operation"] == {"type": "update", "target": table.name},
        )
        assert lock_error["connection_id"] != holder_long["connection_id"]
        assert lock_error["transaction_id"] != holder_transaction_id
        holder_snapshot = next(snapshot for snapshot in lock_error["active_transactions"] if snapshot["transaction_id"] == holder_transaction_id)
        assert holder_snapshot["driver_in_transaction"] is True
        assert any(frame["function"] == "hold_write" for frame in holder_snapshot["owner_task"]["await"])
        assert any(frame["file"] == "tests/integration/test_database_sqlite_transactions.py" for frame in holder_snapshot["source"])

        release_holder.set()
        await asyncio.wait_for(holder_task, timeout=2)
        holder_task = None

        async with AsyncSessionLocal() as writer:
            await writer.execute(update(table).where(table.c.id == 1).values(value="committed"))
            await writer.commit()
        async with AsyncSessionLocal() as reader:
            assert await reader.scalar(select(table.c.value).where(table.c.id == 1)) == "committed"
        assert secret not in json.dumps(sqlite_diagnostic_logs.records)
    finally:
        release_holder.set()
        if holder_task is not None:
            await asyncio.wait_for(holder_task, timeout=2)
        await _drop_probe_table(table)


@pytest.mark.asyncio
async def test_sqlite_diagnostic_tracks_orm_commit_execution_task(sqlite_diagnostic_logs: _DiagnosticCapture) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite transaction diagnostics only")

    await _create_system_setting_table()
    _clear_diagnostics(sqlite_diagnostic_logs)
    owner_task_id = id(asyncio.current_task())
    setting_key = f"diagnostic_{uuid.uuid4().hex}"
    session = AsyncSessionLocal()
    try:
        session.add(SystemSetting(key=setting_key, value="persisted"))
        await session.commit()
        await session.close()

        write_wait = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "write_wait" and record["pending_operation"] == {"type": "insert", "target": "system_setting"},
        )
        transaction_id = write_wait["transaction_id"]
        write_completed = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "write_completed" and record["transaction_id"] == transaction_id,
        )
        returned = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "connection_returned" and record["transaction_id"] == transaction_id,
        )
        assert write_wait["session_id"]
        assert write_wait["owner_task"]["task_id"] == owner_task_id
        assert write_wait["execution_task"]["task_id"] != owner_task_id
        assert write_completed["execution_task"]["task_id"] != owner_task_id
        assert returned["driver_in_transaction"] is False
        assert any(frame["file"] == "tests/integration/test_database_sqlite_transactions.py" for frame in write_wait["source"])

        async with AsyncSessionLocal() as reader:
            assert await reader.scalar(select(SystemSetting.value).where(SystemSetting.key == setting_key)) == "persisted"
    finally:
        await session.close()
        await _drop_system_setting_table()


@pytest.mark.asyncio
async def test_sqlite_read_only_diagnostic_state_expires_after_close(sqlite_diagnostic_logs: _DiagnosticCapture) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite transaction diagnostics only")

    table = _new_probe_table("readonly_probe")
    await _create_probe_table(table)
    _clear_diagnostics(sqlite_diagnostic_logs)
    read_session = AsyncSessionLocal()
    holder_task: asyncio.Task[None] | None = None
    release_holder = asyncio.Event()
    holder_ready = asyncio.Event()
    try:
        await read_session.execute(select(table.c.value).where(table.c.id == 1))
        read_long = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "long_transaction" and record["last_successful_operation"]["type"] == "select",
        )
        read_transaction_id = read_long["transaction_id"]
        assert read_long["driver_in_transaction"] is False
        assert not any(record["event"] in {"write_wait", "write_completed"} and record["transaction_id"] == read_transaction_id for record in sqlite_diagnostic_logs.records)
        await read_session.close()
        _clear_diagnostics(sqlite_diagnostic_logs)

        async def hold_after_close() -> None:
            async with AsyncSessionLocal() as holder:
                await holder.execute(update(table).where(table.c.id == 1).values(value="held"))
                holder_ready.set()
                await release_holder.wait()
                await holder.rollback()

        holder_task = asyncio.create_task(hold_after_close())
        await asyncio.wait_for(holder_ready.wait(), timeout=2)
        new_long = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "long_transaction" and record["transaction_id"] != read_transaction_id,
        )

        async with AsyncSessionLocal() as contender:
            await contender.execute(text("PRAGMA busy_timeout=1"))
            with pytest.raises(OperationalError):
                await contender.execute(update(table).where(table.c.id == 1).values(value="contender"))
            await contender.rollback()
        lock_error = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "lock_error" and record["transaction_id"] != new_long["transaction_id"],
        )
        assert read_transaction_id not in {snapshot["transaction_id"] for snapshot in lock_error["active_transactions"]}
        assert any(snapshot["transaction_id"] == new_long["transaction_id"] for snapshot in lock_error["active_transactions"])
        assert not any(record["event"] == "long_transaction" and record["transaction_id"] == read_transaction_id for record in sqlite_diagnostic_logs.records)
    finally:
        await read_session.close()
        release_holder.set()
        if holder_task is not None:
            await asyncio.wait_for(holder_task, timeout=2)
        await _drop_probe_table(table)


@pytest.mark.asyncio
async def test_sqlite_diagnostic_resamples_after_long_read_becomes_writer(sqlite_diagnostic_logs: _DiagnosticCapture) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite transaction diagnostics only")

    table = _new_probe_table("resample_probe")
    await _create_probe_table(table)
    _clear_diagnostics(sqlite_diagnostic_logs)
    allow_write = asyncio.Event()
    release_holder = asyncio.Event()
    holder_task: asyncio.Task[None] | None = None

    async def hold_read_then_write() -> None:
        async with AsyncSessionLocal() as holder:
            await holder.execute(select(table.c.value).where(table.c.id == 1))
            await allow_write.wait()
            await holder.execute(update(table).where(table.c.id == 1).values(value="transient"))
            await release_holder.wait()
            await holder.rollback()

    try:
        holder_task = asyncio.create_task(hold_read_then_write())
        read_long = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "long_transaction" and record["last_successful_operation"]["type"] == "select" and record["owner_task"]["task_id"] == id(holder_task),
        )
        read_transaction_id = read_long["transaction_id"]
        assert read_long["driver_in_transaction"] is False
        assert read_long["post_write_seconds"] is None
        read_wait_line = next(frame["line"] for frame in read_long["owner_task"]["await"] if frame["function"] == "hold_read_then_write")

        allow_write.set()
        write_completed = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "write_completed" and record["transaction_id"] == read_transaction_id,
        )
        new_long = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "long_transaction" and record["transaction_id"] == read_transaction_id and record["post_write_seconds"] is not None,
        )
        assert new_long["post_write_seconds"] > 0
        assert new_long["driver_in_transaction"] is True
        assert new_long["last_successful_operation"] == {"type": "update", "target": table.name}
        assert new_long["connection_id"] == read_long["connection_id"]
        assert new_long["session_id"] == read_long["session_id"]
        assert new_long["owner_task"]["task_id"] == id(holder_task)
        new_wait_line = next(frame["line"] for frame in new_long["owner_task"]["await"] if frame["function"] == "hold_read_then_write")
        assert new_wait_line != read_wait_line
        assert write_completed["last_successful_operation"] == {"type": "update", "target": table.name}

        release_holder.set()
        await asyncio.wait_for(holder_task, timeout=2)
        holder_task = None

        async with AsyncSessionLocal() as reader:
            assert await reader.scalar(select(table.c.value).where(table.c.id == 1)) == "initial"
    finally:
        allow_write.set()
        release_holder.set()
        if holder_task is not None:
            await asyncio.wait_for(holder_task, timeout=2)
        await _drop_probe_table(table)


@pytest.mark.parametrize("cleanup", ("close", "invalidate"))
@pytest.mark.asyncio
async def test_sqlite_diagnostic_cleans_up_before_long_timer(
    sqlite_diagnostic_logs: _DiagnosticCapture,
    cleanup: str,
) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite transaction diagnostics only")

    table = _new_probe_table("early_cleanup_probe")
    await _create_probe_table(table)
    _clear_diagnostics(sqlite_diagnostic_logs)
    session = AsyncSessionLocal()
    holder_task: asyncio.Task[None] | None = None
    holder_ready = asyncio.Event()
    release_holder = asyncio.Event()
    try:
        await session.execute(update(table).where(table.c.id == 1).values(value="discarded"))
        old_write = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "write_completed" and record["last_successful_operation"] == {"type": "update", "target": table.name},
        )
        old_transaction_id = old_write["transaction_id"]
        await getattr(session, cleanup)()
        _clear_diagnostics(sqlite_diagnostic_logs)

        async with AsyncSessionLocal() as reader:
            assert await reader.scalar(select(table.c.value).where(table.c.id == 1)) == "initial"

        async def hold_new_write() -> None:
            async with AsyncSessionLocal() as holder:
                await holder.execute(update(table).where(table.c.id == 1).values(value="held"))
                holder_ready.set()
                await release_holder.wait()
                await holder.rollback()

        holder_task = asyncio.create_task(hold_new_write())
        await asyncio.wait_for(holder_ready.wait(), timeout=2)
        holder_write = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "write_completed" and record["last_successful_operation"] == {"type": "update", "target": table.name},
        )
        holder_transaction_id = holder_write["transaction_id"]
        await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "long_transaction" and record["transaction_id"] == holder_transaction_id,
        )

        async with AsyncSessionLocal() as contender:
            await contender.execute(text("PRAGMA busy_timeout=1"))
            with pytest.raises(OperationalError):
                await contender.execute(update(table).where(table.c.id == 1).values(value="contender"))
            await contender.rollback()
        lock_error = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "lock_error" and record["transaction_id"] != holder_transaction_id,
        )
        assert old_transaction_id not in {snapshot["transaction_id"] for snapshot in lock_error["active_transactions"]}
        assert any(snapshot["transaction_id"] == holder_transaction_id for snapshot in lock_error["active_transactions"])
        long_transaction_ids = {record["transaction_id"] for record in sqlite_diagnostic_logs.records if record["event"] == "long_transaction"}
        assert long_transaction_ids == {holder_transaction_id}

        release_holder.set()
        await asyncio.wait_for(holder_task, timeout=2)
        holder_task = None
        async with AsyncSessionLocal() as writer:
            await writer.execute(update(table).where(table.c.id == 1).values(value="persisted"))
            await writer.commit()
        async with AsyncSessionLocal() as reader:
            assert await reader.scalar(select(table.c.value).where(table.c.id == 1)) == "persisted"
    finally:
        await session.close()
        release_holder.set()
        if holder_task is not None:
            await asyncio.wait_for(holder_task, timeout=2)
        await _drop_probe_table(table)


@pytest.mark.asyncio
async def test_sqlite_busy_snapshot_diagnostic_preserves_extended_error(sqlite_diagnostic_logs: _DiagnosticCapture) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite transaction diagnostics only")

    table = _new_probe_table("snapshot_probe")
    await _create_probe_table(table)
    _clear_diagnostics(sqlite_diagnostic_logs)
    try:
        async with AsyncSessionLocal() as first:
            await ensure_sqlite_outer_transaction(first)
            assert await first.scalar(select(table.c.value).where(table.c.id == 1)) == "initial"
            async with AsyncSessionLocal() as second:
                await second.execute(update(table).where(table.c.id == 1).values(value="second"))
                await second.commit()

            with pytest.raises(OperationalError) as error_info:
                await first.execute(update(table).where(table.c.id == 1).values(value="first"))
            assert isinstance(error_info.value.orig, sqlite3.OperationalError)
            assert error_info.value.orig.sqlite_errorcode == 517
            assert error_info.value.orig.sqlite_errorcode & 0xFF == 5
            lock_error = await _wait_for_diagnostic(
                sqlite_diagnostic_logs,
                lambda record: record["event"] == "lock_error" and record["sqlite_error_code"] == 517,
            )
            assert lock_error["sqlite_error_code"] & 0xFF == 5
            assert lock_error["sqlite_error_name"] == "SQLITE_BUSY_SNAPSHOT"
            assert lock_error["operation"] == {"type": "update", "target": table.name}
            await first.rollback()

        async with AsyncSessionLocal() as reader:
            assert await reader.scalar(select(table.c.value).where(table.c.id == 1)) == "second"
    finally:
        await _drop_probe_table(table)


@pytest.mark.asyncio
async def test_sqlite_diagnostic_redacts_raw_sql_and_parameters(sqlite_diagnostic_logs: _DiagnosticCapture) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite transaction diagnostics only")

    table = _new_probe_table("redaction_probe")
    await _create_probe_table(table)
    _clear_diagnostics(sqlite_diagnostic_logs)
    secret = f"private-{uuid.uuid4().hex}"
    raw_sql = f"/* {secret} */ UPDATE {table.name} SET value = :secret WHERE id = :row_id"
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text(raw_sql), {"secret": secret, "row_id": 1})
            await session.rollback()

        write_wait = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "write_wait" and record["pending_operation"] == {"type": "update", "target": None},
        )
        transaction_id = write_wait["transaction_id"]
        write_completed = await _wait_for_diagnostic(
            sqlite_diagnostic_logs,
            lambda record: record["event"] == "write_completed" and record["transaction_id"] == transaction_id,
        )
        assert write_completed["driver_in_transaction"] is True
        relevant_records = [record for record in sqlite_diagnostic_logs.records if record["transaction_id"] == transaction_id]
        serialized = json.dumps(relevant_records)
        assert raw_sql not in serialized
        assert secret not in serialized
        assert all("parameters" not in record for record in relevant_records)
        for record in relevant_records:
            for field in ("pending_operation", "last_successful_operation", "operation"):
                operation = record.get(field)
                if operation is not None:
                    assert set(operation) == {"type", "target"}
                    assert operation["type"] == "update"
                    assert operation["target"] is None

        async with AsyncSessionLocal() as reader:
            assert await reader.scalar(select(table.c.value).where(table.c.id == 1)) == "initial"
    finally:
        await _drop_probe_table(table)


@pytest.mark.asyncio
async def test_sqlite_read_then_write_survives_concurrent_commit() -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite transaction semantics only")

    table_name = f"transaction_upgrade_probe_{uuid.uuid4().hex}"
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f"CREATE TABLE {table_name} (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
        await connection.exec_driver_sql(f"INSERT INTO {table_name} (id, value) VALUES (1, 0)")

    try:
        async with AsyncSessionLocal() as first:
            assert await first.scalar(text(f"SELECT value FROM {table_name} WHERE id = 1")) == 0

            async with AsyncSessionLocal() as second:
                await second.execute(text(f"UPDATE {table_name} SET value = 1 WHERE id = 1"))
                await second.commit()

            await first.execute(text(f"UPDATE {table_name} SET value = 2 WHERE id = 1"))
            await first.commit()
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table_name}")


@pytest.mark.parametrize("operation", ("execute", "scalar"))
@pytest.mark.asyncio
async def test_cancelled_sqlite_query_rolls_back_and_allows_next_writer(operation: str) -> None:
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite cancellation semantics only")

    table = _new_probe_table("cancelled_query_probe")
    await _create_probe_table(table)
    loop = asyncio.get_running_loop()
    query_entered = asyncio.Event()
    release = threading.Event()
    session = AsyncSessionLocal()
    query_task: asyncio.Task[Any] | None = None
    underlying_driver = None

    def gate() -> str:
        loop.call_soon_threadsafe(query_entered.set)
        release.wait(timeout=5)
        return "released"

    try:
        async with session:
            connection = await session.connection()
            raw_connection = await connection.get_raw_connection()
            underlying_driver = raw_connection.driver_connection
            await underlying_driver.create_function("gate", 0, gate)

            await session.execute(update(table).where(table.c.id == 1).values(value="uncommitted"))
            query_task = asyncio.create_task(getattr(session, operation)(text("SELECT gate()")))
            await asyncio.wait_for(query_entered.wait(), timeout=2)
            query_task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await query_task
            query_task = None

        async with AsyncSessionLocal() as reader:
            assert await reader.scalar(select(table.c.value).where(table.c.id == 1)) == "initial"

        async with AsyncSessionLocal() as writer:
            await writer.execute(text("PRAGMA busy_timeout=100"))
            await writer.execute(update(table).where(table.c.id == 1).values(value="committed"))
            await writer.commit()

        async with AsyncSessionLocal() as reader:
            assert await reader.scalar(select(table.c.value).where(table.c.id == 1)) == "committed"
    finally:
        release.set()
        if query_task is not None:
            if not query_task.done():
                query_task.cancel()
            try:
                await asyncio.wait_for(query_task, timeout=2)
            except (asyncio.CancelledError, TimeoutError):
                pass
        try:
            await session.close()
        finally:
            if underlying_driver is not None:
                try:
                    await underlying_driver.rollback()
                except Exception:
                    pass
            try:
                await _drop_probe_table(table)
            finally:
                if underlying_driver is not None:
                    try:
                        await underlying_driver.close()
                    except Exception:
                        pass
