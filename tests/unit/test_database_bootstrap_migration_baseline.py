from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.providers.database.bootstrap as bootstrap
from app.models.user import User


@pytest_asyncio.fixture
async def isolated_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], Path]]:
    database_path = tmp_path / "database.db"
    migration_scripts_dir = tmp_path / "migrations"
    migration_scripts_dir.mkdir()
    database_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )
    monkeypatch.setattr(bootstrap, "engine", database_engine)
    monkeypatch.setattr(bootstrap, "MIGRATION_SCRIPTS_DIR", migration_scripts_dir)

    try:
        yield async_sessionmaker(database_engine, expire_on_commit=False), migration_scripts_dir
    finally:
        await database_engine.dispose()


def _write_migration_script(migration_scripts_dir: Path, script_name: str, source: str) -> None:
    (migration_scripts_dir / script_name).write_text(source, encoding="utf-8")


@pytest.mark.asyncio
async def test_init_database_schema_marks_fresh_migrations_without_importing_scripts(isolated_database):
    session_factory, migration_scripts_dir = isolated_database
    migrations = {
        "migration_001_first.py": """
MIGRATION_ID = "fresh_migration_001_v1"


async def migrate(session):
    raise RuntimeError("fresh migration function must not execute")


raise RuntimeError("fresh migration module must not be imported")
""",
        "migration_002_second.py": """
MIGRATION_ID = "fresh_migration_002_v1"


async def migrate(session):
    raise RuntimeError("fresh migration function must not execute")


raise RuntimeError("fresh migration module must not be imported")
""",
    }
    for script_name, source in migrations.items():
        _write_migration_script(migration_scripts_dir, script_name, source)

    async with session_factory() as session:
        await bootstrap.init_database_schema(session)
        await bootstrap.init_database_schema(session)

        migration_records = (await session.execute(text("SELECT migration_id, script_name FROM migration_record ORDER BY migration_id"))).all()
        trigger_names = (await session.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_knowledge_base_collection_owner_%' ORDER BY name"))).scalars().all()

    assert migration_records == [
        ("fresh_migration_001_v1", "migration_001_first.py"),
        ("fresh_migration_002_v1", "migration_002_second.py"),
    ]
    assert trigger_names == [
        "trg_knowledge_base_collection_owner_after_insert",
        "trg_knowledge_base_collection_owner_after_update",
        "trg_knowledge_base_collection_owner_before_insert",
        "trg_knowledge_base_collection_owner_before_update",
    ]


@pytest.mark.asyncio
async def test_init_database_schema_runs_migrations_for_historical_superuser_without_setup_status(
    isolated_database,
):
    session_factory, migration_scripts_dir = isolated_database
    script_name = "migration_003_historical_probe.py"
    migration_id = "historical_migration_003_v1"

    await bootstrap.create_database_tables()
    async with session_factory() as session:
        session.add(User(uid="historical-admin", username="historical-admin", is_superuser=True))
        await session.commit()
        setup_status = (await session.execute(text("SELECT value FROM system_setting WHERE key = 'setup_status'"))).scalar_one_or_none()

    _write_migration_script(
        migration_scripts_dir,
        script_name,
        f"""
from sqlalchemy import text


MIGRATION_ID = {migration_id!r}


async def migrate(session):
    await session.execute(
        text("CREATE TABLE migration_execution_marker (value TEXT NOT NULL)")
    )
    await session.execute(
        text("INSERT INTO migration_execution_marker (value) VALUES (:value)"),
        {{"value": "executed"}},
    )
""",
    )

    async with session_factory() as session:
        await bootstrap.init_database_schema(session)

        marker_value = (await session.execute(text("SELECT value FROM migration_execution_marker"))).scalar_one()
        migration_record = (await session.execute(text("SELECT migration_id, script_name FROM migration_record"))).one()

    assert setup_status is None
    assert marker_value == "executed"
    assert migration_record == (migration_id, script_name)
