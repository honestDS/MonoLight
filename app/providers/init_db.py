from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.providers.database import Base, engine
from app.schemas.profile import ProfileConfig


def merge_configs(base: dict, target: dict) -> dict:
    """递归合并配置字典，确保目标字典包含基础字典的所有键"""
    for key, value in base.items():
        if key not in target:
            target[key] = value
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_configs(value, target[key])
    return target


async def init_system_data(session: AsyncSession):
    # 1. 确保物理表结构存在
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. 获取当前最新的默认配置模版
    # 构造一个空的 ProfileConfig 以获取所有默认值
    default_config_obj = ProfileConfig(
        provider={"model_id": "gemini-1.5-flash"},  # model_id 是必须项
        security={},
        tool={},
        other={},
    )
    latest_default_configs = default_config_obj.model_dump()

    # 3. 初始化默认 Prompt
    prompt_res = await session.execute(
        select(PromptLibrary).where(PromptLibrary.name == "default")
    )
    prompt_obj = prompt_res.scalars().first()
    if not prompt_obj:
        prompt_obj = PromptLibrary(name="default", content="", uid=None)
        session.add(prompt_obj)
        await session.flush()

    # 4. 遍历并升级所有现有的 Profile 配置
    profile_res = await session.execute(select(Profile))
    all_profiles = profile_res.scalars().all()

    for profile in all_profiles:
        current_configs = profile.configs or {}
        # 执行深度合并，补充缺失字段
        updated_configs = merge_configs(latest_default_configs, current_configs)
        profile.configs = updated_configs

    # 5. 如果没有任何 Profile，创建一个默认的
    if not all_profiles:
        default_profile = Profile(
            name="default",
            provider_id=-1,
            prompt_id=prompt_obj.id,
            configs=latest_default_configs,
            is_active=True,
        )
        session.add(default_profile)

    await session.commit()
