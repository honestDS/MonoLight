import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

import app.models  # noqa
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud
from app.core.crud.system_setting import system_setting_crud
from app.core.crud.user import user_crud
from app.models.active_session import ActiveSession
from app.models.profile import (
    ProfileConfig,
)

from .client import engine

logger = logging.getLogger("uvicorn.error")


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
            "is_active": True,
        },
    )


async def init_system_data(session: AsyncSession):
    # 1. 基础表初始化 (若表不存在则创建)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    # 清空会话锁表 (active_session)
    await session.execute(text(f"DELETE FROM {ActiveSession.__tablename__}"))
    await session.commit()
    logger.info("INIT: active_session table cleared")

    # 2. 业务配置初始化
    await system_setting_crud.ensure_defaults(session)

    prompt_obj = await prompt_crud.get_by_name(session, name="default")
    if not prompt_obj:
        prompt_obj = await prompt_crud.create(session, obj_in={"name": "default", "content": "", "uid": None})

    admin_username = os.getenv("ADMIN_USERNAME", "admin")
    admin_user = await user_crud.get_by_username(session, admin_username)
    admin_uid = admin_user.uid if admin_user else None

    await ensure_default_profile_for_user(session, admin_uid)

    await session.commit()
