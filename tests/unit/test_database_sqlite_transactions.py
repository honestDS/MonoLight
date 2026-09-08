import uuid

import pytest
from sqlalchemy import text

from app.providers.database import AsyncSessionLocal, engine, ensure_sqlite_outer_transaction


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
