from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud
from app.core.crud.provider import provider_crud
from app.core.exceptions import (
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.security import get_current_user
from app.models.profile import (
    ProfileCreate,
    ProfileResponse,
    ProfileUpdate,
)
from app.models.provider import ModelUsage
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


async def validate_rerank_provider(db: AsyncSession, provider_config: dict) -> None:
    """rerank 启用判定：配置了 rerank_provider_id 与 rerank_model_id 即视为启用。

    - 两者均未配置：视为未启用 rerank，直接跳过校验。
    - 仅配置其一：视为配置不完整，拦截并提示补全。
    - 两者均配置：强校验 provider 存在且 usage 为 RERANK，并校验候选数量 K。
    """
    rerank_provider_id = provider_config.get("rerank_provider_id")
    rerank_model_id = provider_config.get("rerank_model_id")
    has_provider = bool(rerank_provider_id) and rerank_provider_id > 0
    has_model = bool(rerank_model_id)

    if not has_provider and not has_model:
        return

    if not has_provider or not has_model:
        raise ParameterException(constants.ERR_PROFILE_RERANK_CONFIG_INCOMPLETE)

    provider = await provider_crud.get(db, rerank_provider_id)
    if not provider:
        raise ParameterException(constants.ERR_PROFILE_RERANK_PROVIDER_NOT_FOUND)
    if provider.usage != ModelUsage.RERANK:
        raise ParameterException(constants.ERR_PROFILE_PROVIDER_NOT_RERANK)

    # 候选数量 K 必须大于等于知识库返回数量，否则精排无法改变最终返回集合，rerank 失去意义
    rerank_candidate_k = provider_config.get("rerank_candidate_k", 20)
    kb_query_top_k = provider_config.get("kb_query_top_k", 5)
    if isinstance(rerank_candidate_k, int) and isinstance(kb_query_top_k, int) and rerank_candidate_k < kb_query_top_k:
        raise ParameterException(constants.ERR_PROFILE_RERANK_CANDIDATE_K_TOO_SMALL)




@router.post("/create", response_model=StandardResponse[ProfileResponse])
async def create_profile(
    profile_in: ProfileCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    provider_id = profile_in.configs.get("provider", {}).get("provider_id")
    if provider_id and provider_id > 0:
        if not await provider_crud.get(db, provider_id):
            raise ParameterException(constants.ERR_PROVIDER_NOT_FOUND)

    embedding_provider_id = profile_in.configs.get("provider", {}).get("embedding_provider_id")
    if embedding_provider_id and embedding_provider_id > 0:
        if not await provider_crud.get(db, embedding_provider_id):
            raise ParameterException(constants.ERR_PROFILE_EMBEDDING_PROVIDER_NOT_FOUND)

    await validate_rerank_provider(db, profile_in.configs.get("provider", {}))

    if await profile_crud.get_by_name(db, profile_in.name):
        raise ParameterException(constants.ERR_PROFILE_NAME_EXISTS)


    if profile_in.prompt_id:
        if not await prompt_crud.get(db, profile_in.prompt_id):
            raise ParameterException(constants.ERR_PROMPT_NOT_FOUND)

    db_profile = await profile_crud.create(db, obj_in=profile_in)
    # Re-fetch with relations
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = ProfileResponse.model_validate(db_profile)
    provider_id = db_profile.configs.get("provider", {}).get("provider_id")
    if provider_id:
        provider = await provider_crud.get(db, provider_id)
        if provider:
            res_data.provider_name = provider.name
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
        provider_id = p.configs.get("provider", {}).get("provider_id")
        if provider_id:
            provider = await provider_crud.get(db, provider_id)
            if provider:
                item.provider_name = provider.name
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

    provider_id = profile.configs.get("provider", {}).get("provider_id")
    if not provider_id or provider_id <= 0:
        raise ParameterException(constants.ERR_ACTIVATE_NO_PROVIDER)

    if not await provider_crud.get(db, provider_id):
        raise ParameterException(constants.ERR_PROVIDER_NOT_FOUND)

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
        provider_id = profile_in.configs.get("provider", {}).get("provider_id")
        if provider_id and provider_id > 0:
            if not await provider_crud.get(db, provider_id):
                raise ParameterException(constants.ERR_PROVIDER_NOT_FOUND)

        embedding_provider_id = profile_in.configs.get("provider", {}).get("embedding_provider_id")
        if embedding_provider_id and embedding_provider_id > 0:
            if not await provider_crud.get(db, embedding_provider_id):
                raise ParameterException(constants.ERR_PROFILE_EMBEDDING_PROVIDER_NOT_FOUND)

        await validate_rerank_provider(db, profile_in.configs.get("provider", {}))

    if profile_in.name and profile_in.name != db_profile.name:

        if await profile_crud.get_by_name(db, profile_in.name):
            raise ParameterException(constants.ERR_PROFILE_NAME_EXISTS)

    if profile_in.prompt_id:
        if not await prompt_crud.get(db, profile_in.prompt_id):
            raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)

    db_profile = await profile_crud.update(db, db_obj=db_profile, obj_in=profile_in)
    # Re-fetch with relations
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = ProfileResponse.model_validate(db_profile)
    provider_id = db_profile.configs.get("provider", {}).get("provider_id")
    if provider_id:
        provider = await provider_crud.get(db, provider_id)
        if provider:
            res_data.provider_name = provider.name
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

    # 简单统计逻辑依然可以保留
    count = len(await profile_crud.get_multi(db))
    if count <= 1:
        raise ParameterException(constants.ERR_DELETE_LAST_PROFILE)

    if db_profile.is_active:
        raise ParameterException(constants.ERR_DELETE_ACTIVE_PROFILE)

    await profile_crud.remove(db, id=profile_id)
    return StandardResponse.success(message=constants.MSG_PROFILE_DELETED)
