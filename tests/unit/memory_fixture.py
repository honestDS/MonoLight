from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.database_support import clone_sqlite_schema
from tests.unit.memory_test_support import MEMORY_TABLES, MemoryVectorBackend


@pytest_asyncio.fixture
async def memory_session_factory(
    tmp_path: Any,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "memory-memory.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )
    await clone_sqlite_schema(database_path, tables=MEMORY_TABLES)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.fixture
def vector_backend(monkeypatch: pytest.MonkeyPatch) -> MemoryVectorBackend:
    from app.core.memory_jobs import (
        maintenance_lifecycle,
        maintenance_state,
        maintenance_vector,
        migration_handler,
    )

    backend = MemoryVectorBackend()
    monkeypatch.setattr(maintenance_state, "load_embedding_runtime_config", backend.load_config)
    monkeypatch.setattr(maintenance_vector, "embed_texts_with_config", backend.embed)
    monkeypatch.setattr(maintenance_vector, "async_validate_collection", backend.validate)
    monkeypatch.setattr(maintenance_vector, "async_get_or_create_collection", backend.get_or_create_collection)
    monkeypatch.setattr(maintenance_vector, "async_delete_collection", backend.delete_collection)
    monkeypatch.setattr(maintenance_vector, "async_upsert_collection_items", backend.upsert)
    monkeypatch.setattr(maintenance_vector, "async_get_collection_items", backend.get_items)
    monkeypatch.setattr(maintenance_vector, "async_delete_orphan_items", backend.delete_orphans)
    monkeypatch.setattr(maintenance_vector, "hybrid_query_collection", backend.hybrid_query)
    monkeypatch.setattr(maintenance_lifecycle, "async_validate_collection", backend.validate)
    monkeypatch.setattr(maintenance_lifecycle, "async_delete_collection", backend.delete_collection)
    monkeypatch.setattr(migration_handler, "async_delete_collection_items", backend.delete_items)
    return backend


__all__ = (
    "MEMORY_TABLES",
    "MemoryVectorBackend",
    "memory_session_factory",
    "vector_backend",
)
