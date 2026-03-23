from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel
from app.models.profile import Profile, ProfileConfig
from app.models.prompt import PromptLibrary
from app.providers.database import engine

# CRUD Imports
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud

def merge_configs(base: dict, target: dict) -> dict:
    for key, value in base.items():
        if key not in target:
            target[key] = value
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_configs(value, target[key])
    return target

async def init_system_data(session: AsyncSession):
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    default_config_obj = ProfileConfig(
        provider={"model_id": "gemini-1.5-flash"},
        security={},
        tool={},
        other={},
    )
    latest_default_configs = default_config_obj.model_dump()

    # 使用 CRUD 检查并初始化默认 Prompt
    prompt_obj = await prompt_crud.get_by_name(session, name="default")
    if not prompt_obj:
        prompt_obj = await prompt_crud.create(session, obj_in={
            "name": "default",
            "content": "",
            "uid": None
        })

    # 使用 CRUD 获取所有 Profile
    all_profiles = await profile_crud.get_multi(session, limit=100)

    for profile in all_profiles:
        current_configs = profile.configs or {}
        # 递归合并配置以保持向后兼容
        updated_configs = merge_configs(latest_default_configs, current_configs)
        await profile_crud.update(session, db_obj=profile, obj_in={"configs": updated_configs})

    if not all_profiles:
        # 使用 CRUD 创建默认 Profile
        await profile_crud.create(session, obj_in={
            "name": "default",
            "provider_id": -1,
            "prompt_id": prompt_obj.id,
            "configs": latest_default_configs,
            "is_active": True,
        })

    await session.commit()
