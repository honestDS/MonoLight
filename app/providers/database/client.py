import asyncio
import os
import sys
from collections.abc import Awaitable

from dotenv import load_dotenv
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.paths import SQLITE_DB_PATH, TEST_SESSION_DB_PATH, ensure_data_dirs

load_dotenv()


async def _await_cancellation_safe[ResultT](awaitable: Awaitable[ResultT]) -> ResultT:
    operation = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
        if not operation.cancelled():
            operation.exception()
        raise


class CancellationSafeAsyncSession(AsyncSession):
    async def commit(self) -> None:
        await _await_cancellation_safe(super().commit())

    async def rollback(self) -> None:
        await _await_cancellation_safe(super().rollback())

    async def close(self) -> None:
        await _await_cancellation_safe(super().close())


ensure_data_dirs()
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    DATABASE_URL = f"sqlite+aiosqlite:///{SQLITE_DB_PATH}"

if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
    DATABASE_URL = f"sqlite+aiosqlite:///{TEST_SESSION_DB_PATH}"

_engine_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
}
if DATABASE_URL.startswith("sqlite+"):
    _engine_kwargs["connect_args"] = {"timeout": 30}

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)


if DATABASE_URL.startswith("sqlite+"):

    @event.listens_for(engine.sync_engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


AsyncSessionLocal = async_sessionmaker(bind=engine, class_=CancellationSafeAsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
