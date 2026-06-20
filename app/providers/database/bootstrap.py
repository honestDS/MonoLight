import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

import app.models  # noqa
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud
from app.models.active_session import ActiveSession
from app.models.profile import (
    ProfileConfig,
)

from .client import engine

logger = logging.getLogger("uvicorn.error")


async def run_data_migrations() -> None:
    """执行业务数据迁移。

    框架只保留入口，具体迁移逻辑由独立的数据迁移脚本实现。
    保持为空函数，避免在启动流程中耦合历史数据清洗。
    """
    # TODO: 在独立数据迁移脚本中实现历史数据迁移逻辑
    return None


def merge_configs(base: dict, target: dict) -> dict:
    for key, value in base.items():
        if key not in target:
            target[key] = value
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_configs(value, target[key])
    return target


async def init_system_data(session: AsyncSession):
    # 1. 基础表初始化 (若表不存在则创建)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    # 清空会话锁表 (active_session)
    await session.execute(text(f"DELETE FROM {ActiveSession.__tablename__}"))
    await session.commit()
    logger.info("INIT: active_session table cleared")

    # 2. 业务配置初始化
    default_config_obj = ProfileConfig(
        channel={},
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
                "prompt_id": prompt_obj.id,
                "configs": latest_default_configs,
                "is_active": True,
            },
        )

    await session.commit()
