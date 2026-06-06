import logging

from sqlalchemy import (
    inspect,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

# 强制导入所有模型以注册 Metadata
import app.models  # noqa

# CRUD Imports
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud
from app.models.profile import (
    ProfileConfig,
)
from app.providers.database import engine

logger = logging.getLogger("uvicorn.error")


async def sync_database_schema():
    """
    通用数据库架构同步引擎 (支持 SQLite, MySQL, PostgreSQL 等)
    使用 SQLAlchemy Inspector 实现跨数据库的物理列自动发现与补全。
    """
    dialect_name = engine.dialect.name

    async with engine.connect() as conn:

        def get_inspector(sync_conn):
            return inspect(sync_conn)

        # 在异步连接中运行同步检查
        inspector = await conn.run_sync(get_inspector)

        for table_name, table_obj in SQLModel.metadata.tables.items():
            # 检查表是否存在
            def check_table_exists(sync_conn):
                return inspector.has_table(table_name)

            if not await conn.run_sync(check_table_exists):
                continue

            # 获取现有物理列
            def get_columns(sync_conn):
                return inspector.get_columns(table_name)

            existing_columns = await conn.run_sync(get_columns)
            existing_column_names = [col["name"] for col in existing_columns]

            for column in table_obj.columns:
                if column.name not in existing_column_names:
                    # 转换类型 (SQLAlchemy 类型转字符串)
                    # 处理 SQLite 兼容性：SQLite 不支持很多复杂 ALTER，但支持简单 ADD COLUMN
                    col_type_compiled = column.type.compile(dialect=engine.dialect)

                    # 处理默认值
                    default_clause = ""
                    if not column.nullable:
                        if column.default is not None:
                            d_val = column.default.arg
                            if isinstance(d_val, str):
                                default_clause = f" DEFAULT '{d_val}'"
                            else:
                                default_clause = f" DEFAULT {d_val}"
                        else:
                            # 必填项兜底
                            default_clause = " DEFAULT ''"

                    # 构造 ALTER 语句
                    if dialect_name == "mysql":
                        alter_query = f"ALTER TABLE `{table_name}` ADD `{column.name}` {col_type_compiled}{default_clause}"
                    else:
                        alter_query = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {col_type_compiled}{default_clause}"

                    try:
                        await conn.execute(text(alter_query))
                        await conn.commit()
                        logger.info(f"SCHEMA_SYNC: Table {table_name} added column {column.name}")
                    except Exception as e:
                        logger.error(f"SCHEMA_SYNC_ERR: Failed to add {column.name} to {table_name}: {e}")


def merge_configs(base: dict, target: dict) -> dict:
    for key, value in base.items():
        if key not in target:
            target[key] = value
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_configs(value, target[key])
    return target


async def init_system_data(session: AsyncSession):
    # 1. 物理架构同步 (异步化)
    await sync_database_schema()

    # 2. 基础表初始化 (若表不存在则创建)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    # 3. 业务配置初始化
    default_config_obj = ProfileConfig(
        provider={"model_id": "gemini-1.5-flash"},
        security={},
        tool={},
        other={},
    )
    latest_default_configs = default_config_obj.model_dump()

    prompt_obj = await prompt_crud.get_by_name(session, name="default")
    if not prompt_obj:
        prompt_obj = await prompt_crud.create(session, obj_in={"name": "default", "content": "", "uid": None})

    all_profiles = await profile_crud.get_multi(session, limit=100)

    for profile in all_profiles:
        current_configs = profile.configs or {}
        updated_configs = merge_configs(latest_default_configs, current_configs)
        await profile_crud.update(session, db_obj=profile, obj_in={"configs": updated_configs})

    if not all_profiles:
        await profile_crud.create(
            session,
            obj_in={
                "name": "default",
                "provider_id": -1,
                "prompt_id": prompt_obj.id,
                "configs": latest_default_configs,
                "is_active": True,
            },
        )

    await session.commit()
