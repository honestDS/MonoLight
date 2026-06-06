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


@router.post("/create", response_model=StandardResponse[ProfileResponse])
async def create_profile(
    profile_in: ProfileCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    if profile_in.provider_id > 0:
        if not await provider_crud.get(db, profile_in.provider_id):
            raise ParameterException(constants.ERR_PROVIDER_NOT_FOUND)

    if await profile_crud.get_by_name(db, profile_in.name):
        raise ParameterException(constants.ERR_PROFILE_NAME_EXISTS)

    if profile_in.prompt_id:
        if not await prompt_crud.get(db, profile_in.prompt_id):
            raise ParameterException(constants.ERR_PROMPT_NOT_FOUND)

    db_profile = await profile_crud.create(db, obj_in=profile_in)
    # Re-fetch with provider
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = ProfileResponse.model_validate(db_profile)
    if db_profile.provider:
        res_data.provider_name = db_profile.provider.name
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
        # 预加载了 relations 的话可以直接访问
        if hasattr(p, "provider") and p.provider:
            item.provider_name = p.provider.name
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

    if profile.provider_id <= 0:
        raise ParameterException(constants.ERR_ACTIVATE_NO_PROVIDER)

    if not await provider_crud.get(db, profile.provider_id):
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

    if profile_in.provider_id is not None:
        if profile_in.provider_id > 0:
            if not await provider_crud.get(db, profile_in.provider_id):
                raise ParameterException(constants.ERR_PROVIDER_NOT_FOUND)

    if profile_in.name and profile_in.name != db_profile.name:
        if await profile_crud.get_by_name(db, profile_in.name):
            raise ParameterException(constants.ERR_PROFILE_NAME_EXISTS)

    if profile_in.prompt_id:
        if not await prompt_crud.get(db, profile_in.prompt_id):
            raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)

    db_profile = await profile_crud.update(db, db_obj=db_profile, obj_in=profile_in)
    # Re-fetch with relations for provider_name
    db_profile = await profile_crud.get_with_relations(db, db_profile.id)
    res_data = ProfileResponse.model_validate(db_profile)
    if db_profile.provider:
        res_data.provider_name = db_profile.provider.name
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
