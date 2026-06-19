"""Profile API：渠道管理架构适配版

CRUD 支持 chat_channel/embedding_channel/rerank_channel；activate 校验适配
"""

from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud
from app.core.exceptions import (
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.security import get_current_user
from app.models.channel import ChannelConfig
from app.models.profile import (
    ProfileCreate,
    ProfileResponse,
    ProfileUpdate,
)
from app.providers.database import get_db
from app.schemas.response import (
    PageData,
    StandardResponse,
)

router = APIRouter(
    prefix="/profiles",
    tags=["Profile Management"],
    dependencies=[Depends(get_current_user)],
)


async def check_admin_privilege(current_user=Depends(get_current_user)):
    if not getattr(current_user, "is_superuser", False):
        raise ForbiddenException(constants.ERR_ONLY_ADMIN_ALLOWED)
    return current_user


async def validate_channel_configs(provider_config: dict) -> None:
    """校验 provider 配置中的渠道配置合法性。

    - chat_channel：至少需要一条启用规则
    - embedding_channel：可选，有配置则校验
    - rerank_channel：可选，有配置则校验
    """
    chat_channel = provider_config.get("chat_channel")
    if chat_channel:
        try:
            ChannelConfig.model_validate(chat_channel)
        except Exception as e:
            raise ParameterException(f"chat_channel 校验失败: {e}")

    embedding_channel = provider_config.get("embedding_channel")
    if embedding_channel:
        try:
            ChannelConfig.model_validate(embedding_channel)
        except Exception as e:
            raise ParameterException(f"embedding_channel 校验失败: {e}")

    rerank_channel = provider_config.get("rerank_channel")
    if rerank_channel:
        try:
            rerank_config = ChannelConfig.model_validate(rerank_channel)
        except Exception as e:
            raise ParameterException(f"rerank_channel 校验失败: {e}")
        if rerank_config.rerank_candidate_k < rerank_config.kb_query_top_k:
            raise ParameterException(constants.ERR_PROFILE_RERANK_CANDIDATE_K_TOO_SMALL)


@router.post("/create", response_model=StandardResponse[ProfileResponse])
async def create_profile(
    profile_in: ProfileCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    provider_config = profile_in.configs.get("provider", {})
    await validate_channel_configs(provider_config)

    if await profile_crud.get_by_name(db, profile_in.name):
        raise ParameterException(constants.ERR_PROFILE_NAME_EXISTS)

    if profile_in.prompt_id:
        if not await prompt_crud.get(db, profile_in.prompt_id):
            raise ParameterException(constants.ERR_PROMPT_NOT_FOUND)

    db_profile = await profile_crud.create(db, obj_in=profile_in)
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = ProfileResponse.model_validate(db_profile)
    return StandardResponse.success(
        data=res_data,
        message=constants.MSG_PROFILE_CREATED,
    )


@router.get("/list", response_model=StandardResponse)
async def list_profiles(
    page: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
):
    skip = (page - 1) * size
    profiles = await profile_crud.get_multi(db, skip=skip, limit=size)
    total = await profile_crud.count(db)

    results = []
    for p in profiles:
        item = ProfileResponse.model_validate(p)
        results.append(item)

    page_data = PageData(
        items=results,
        total=total,
        page=page,
        size=size,
    )
    return StandardResponse.success(data=page_data)


@router.post("/activate")
async def activate_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    profile = await profile_crud.get(db, profile_id)
    if not profile:
        raise ResourceNotFoundException(constants.ERR_PROFILE_NOT_FOUND)

    # 校验 chat_channel 配置存在
    provider_config = profile.configs.get("provider", {})
    chat_channel = provider_config.get("chat_channel")
    if not chat_channel or not chat_channel.get("rules"):
        raise ParameterException(constants.ERR_ACTIVATE_NO_PROVIDER)

    await db.execute(update(profile_crud.model).values(is_active=False))
    profile.is_active = True
    await db.commit()
    return StandardResponse.success(message=constants.MSG_PROFILE_ACTIVATED)


@router.post("/update", response_model=StandardResponse[ProfileResponse])
async def update_profile(
    profile_id: int,
    profile_in: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_profile = await profile_crud.get(db, profile_id)
    if not db_profile:
        raise ResourceNotFoundException(constants.ERR_PROFILE_NOT_FOUND)

    if profile_in.configs:
        provider_config = profile_in.configs.get("provider", {})
        await validate_channel_configs(provider_config)

    if profile_in.name and profile_in.name != db_profile.name:
        if await profile_crud.get_by_name(db, profile_in.name):
            raise ParameterException(constants.ERR_PROFILE_NAME_EXISTS)

    if profile_in.prompt_id:
        if not await prompt_crud.get(db, profile_in.prompt_id):
            raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)

    db_profile = await profile_crud.update(db, db_obj=db_profile, obj_in=profile_in)
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = ProfileResponse.model_validate(db_profile)
    return StandardResponse.success(
        data=res_data,
        message=constants.MSG_PROFILE_UPDATED,
    )


@router.post("/delete")
async def delete_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_profile = await profile_crud.get(db, profile_id)
    if not db_profile:
        raise ResourceNotFoundException(constants.ERR_PROFILE_NOT_FOUND)

    count = len(await profile_crud.get_multi(db))
    if count <= 1:
        raise ParameterException(constants.ERR_DELETE_LAST_PROFILE)

    if db_profile.is_active:
        raise ParameterException(constants.ERR_DELETE_ACTIVE_PROFILE)

    await profile_crud.remove(db, id=profile_id)
    return StandardResponse.success(message=constants.MSG_PROFILE_DELETED)
