from typing import List
from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from app.core import constants
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
from app.schemas.response import StandardResponse
from app.core.crud.profile import profile_crud
from app.core.crud.provider import provider_crud
from app.core.crud.prompt import prompt_crud

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
    return StandardResponse.success(
        data=ProfileResponse.model_validate(db_profile),
        message=constants.MSG_PROFILE_CREATED,
    )


@router.get("/list", response_model=StandardResponse[List[ProfileResponse]])
async def list_profiles(db: AsyncSession = Depends(get_db)):
    profiles = await profile_crud.get_multi(db)
    return StandardResponse.success(
        data=[ProfileResponse.model_validate(p) for p in profiles]
    )


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
    return StandardResponse.success(
        data=ProfileResponse.model_validate(db_profile),
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
