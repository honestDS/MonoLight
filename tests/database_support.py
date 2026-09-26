import asyncio
import hashlib
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.sql.schema import Table
from sqlmodel import SQLModel

_TEMPLATE_ROOT = tempfile.TemporaryDirectory(prefix="monolight-pytest-schema-")
_TEMPLATE_PATHS: dict[tuple[str, ...], Path] = {}


def _resolve_tables(tables: Iterable[Table] | None) -> tuple[tuple[str, ...], tuple[Table, ...] | None]:
    if tables is None:
        resolved = tuple(SQLModel.metadata.tables.values())
        return tuple(sorted(table.key for table in resolved)), None

    resolved = tuple(tables)
    return tuple(sorted(table.key for table in resolved)), resolved


async def clone_sqlite_schema(database_path: str | Path, *, tables: Iterable[Table] | None = None) -> None:
    target_path = Path(database_path)
    schema_key, resolved_tables = _resolve_tables(tables)
    template_path = _TEMPLATE_PATHS.get(schema_key)

    if template_path is None:
        digest = hashlib.sha256("\0".join(schema_key).encode()).hexdigest()[:16]
        template_path = Path(_TEMPLATE_ROOT.name) / f"schema-{digest}.db"
        template_engine = create_async_engine(f"sqlite+aiosqlite:///{template_path.as_posix()}")

        @event.listens_for(template_engine.sync_engine, "connect")
        def configure_template_connection(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=MEMORY")
                cursor.execute("PRAGMA synchronous=OFF")
            finally:
                cursor.close()

        try:
            async with template_engine.begin() as connection:
                await connection.run_sync(
                    lambda sync_connection: SQLModel.metadata.create_all(
                        sync_connection,
                        tables=resolved_tables,
                    )
                )
        finally:
            await template_engine.dispose()

        _TEMPLATE_PATHS[schema_key] = template_path

    target_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copyfile, template_path, target_path)
