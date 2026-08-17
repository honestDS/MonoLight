import importlib.util
import logging
from pathlib import Path
from types import ModuleType

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

import app.models  # noqa
from app.core.constants import (
    ERR_DATABASE_TYPE_UNSUPPORTED,
    ERR_MIGRATION_FUNCTION_MISSING,
    ERR_MIGRATION_ID_INVALID,
    ERR_MIGRATION_SCRIPT_INVALID,
    SETUP_STATUS_COMPLETED,
)
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud
from app.core.crud.system_setting import system_setting_crud
from app.core.crud.user import user_crud
from app.core.i18n import t
from app.models.profile import (
    ProfileConfig,
)

from .client import engine

logger = logging.getLogger("uvicorn.error")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_SCRIPTS_DIR = PROJECT_ROOT / "scripts"
MIGRATION_RECORD_TABLE = "migration_record"
MIGRATION_FILE_PREFIX = "migration_"
MIGRATION_FILE_SUFFIX = ".py"


def build_default_profile_configs() -> dict:
    default_config_obj = ProfileConfig(
        channel={},
        security={},
        tool={},
        other={},
    )
    return default_config_obj.model_dump()


async def ensure_default_profile_for_user(session: AsyncSession, uid: str | None) -> None:
    if not uid or await profile_crud.get_by_uid(session, uid):
        return

    prompt_obj = await prompt_crud.get_by_name(session, name="default")
    if not prompt_obj:
        prompt_obj = await prompt_crud.create(session, obj_in={"name": "default", "content": "", "uid": None})

    await profile_crud.create(
        session,
        obj_in={
            "name": "default",
            "uid": uid,
            "prompt_id": prompt_obj.id,
            "configs": build_default_profile_configs(),
            "is_default": True,
        },
    )


async def _initialize_setup_state_and_get_admin_uid(session: AsyncSession) -> str | None:
    superuser = await user_crud.get_superuser(session)
    admin_uid = superuser.uid if superuser else None
    status, confirmed_admin_uid = await system_setting_crud.initialize_setup_state(session, admin_uid=admin_uid)
    if status != SETUP_STATUS_COMPLETED or not confirmed_admin_uid:
        return None

    admin_user = await user_crud.get_by_uid(session, confirmed_admin_uid)
    if admin_user and admin_user.is_superuser:
        return admin_user.uid
    return None


async def ensure_migration_record_table(session: AsyncSession) -> None:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "mysql":
        id_definition = "INTEGER PRIMARY KEY AUTO_INCREMENT"
    elif dialect_name == "sqlite":
        id_definition = "INTEGER PRIMARY KEY AUTOINCREMENT"
    else:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=dialect_name))
    await session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {MIGRATION_RECORD_TABLE} (
                id {id_definition},
                migration_id VARCHAR(255) NOT NULL UNIQUE,
                script_name VARCHAR(255) NOT NULL,
                executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            )
            """
        )
    )
    await session.commit()


async def has_migration_executed(session: AsyncSession, migration_id: str) -> bool:
    result = await session.execute(
        text(f"SELECT 1 FROM {MIGRATION_RECORD_TABLE} WHERE migration_id = :migration_id LIMIT 1"),
        {"migration_id": migration_id},
    )
    return result.scalar() is not None


async def mark_migration_executed(session: AsyncSession, migration_id: str, script_name: str) -> None:
    await session.execute(
        text(
            f"""
            INSERT INTO {MIGRATION_RECORD_TABLE} (migration_id, script_name)
            VALUES (:migration_id, :script_name)
            """
        ),
        {"migration_id": migration_id, "script_name": script_name},
    )


def load_migration_module(script_path: Path) -> ModuleType:
    module_name = f"monoligh_migration_{script_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(t(ERR_MIGRATION_SCRIPT_INVALID, script_name=script_path.name))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iter_migration_scripts() -> list[Path]:
    if not MIGRATION_SCRIPTS_DIR.exists():
        return []
    return sorted(path for path in MIGRATION_SCRIPTS_DIR.iterdir() if path.is_file() and path.name.startswith(MIGRATION_FILE_PREFIX) and path.name.endswith(MIGRATION_FILE_SUFFIX))


async def run_once_migration_scripts(session: AsyncSession) -> None:
    await ensure_migration_record_table(session)
    for script_path in iter_migration_scripts():
        module = load_migration_module(script_path)
        migration_id = getattr(module, "MIGRATION_ID", script_path.stem)
        migrate_func = getattr(module, "migrate", None)
        if not isinstance(migration_id, str) or not migration_id.strip():
            raise RuntimeError(t(ERR_MIGRATION_ID_INVALID, script_name=script_path.name))
        if migrate_func is None:
            raise RuntimeError(t(ERR_MIGRATION_FUNCTION_MISSING, script_name=script_path.name))
        if await has_migration_executed(session, migration_id):
            continue

        logger.info("MIGRATION: running %s", migration_id)
        await migrate_func(session)
        await mark_migration_executed(session, migration_id, script_path.name)
        await session.commit()
        logger.info("MIGRATION: completed %s", migration_id)


async def create_database_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def init_database_schema(session: AsyncSession) -> None:
    await create_database_tables()
    await run_once_migration_scripts(session)


async def init_system_data(session: AsyncSession):
    # 1. 基础表初始化与迁移
    await init_database_schema(session)
    confirmed_admin_uid = await _initialize_setup_state_and_get_admin_uid(session)

    # 2. 业务配置初始化
    await system_setting_crud.ensure_defaults(session)

    prompt_obj = await prompt_crud.get_by_name(session, name="default")
    if not prompt_obj:
        prompt_obj = await prompt_crud.create(session, obj_in={"name": "default", "content": "", "uid": None})

    await ensure_default_profile_for_user(session, confirmed_admin_uid)

    await session.commit()
