import asyncio
import sys
import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, select, text, update

from app.providers.database import (
    AsyncSessionLocal,
    engine,
    ensure_sqlite_outer_transaction,
)
from app.providers.database.client import CancellationSafeAsyncSession


@pytest.fixture(autouse=True)
async def _database_factory(database_factory, monkeypatch):
    async with database_factory(
        create_schema=False,
        foreign_keys=True,
        wal=True,
        session_class=CancellationSafeAsyncSession,
    ) as factory:
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
