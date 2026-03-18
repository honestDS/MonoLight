from app.core import constants
from app.core.exceptions import ResourceNotFoundException, ParameterException
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from sqlalchemy.orm import joinedload
from app.providers.database import get_db
from app.models.profile import Profile
from app.models.provider import ModelProvider
from app.models.prompt import PromptLibrary
from app.schemas.profile import ProfileCreate, ProfileResponse, ProfileUpdate
from app.schemas.response import StandardResponse
from app.core.security import get_current_user
async def check_admin_privilege(current_user: dict = Depends(get_current_user)):
    if not current_user.get('is_superuser'):
        from app.core.exceptions import ForbiddenException
        from app.core import constants
        raise ForbiddenException(constants.ERR_ONLY_ADMIN_ALLOWED)
    return current_user
from typing import List

router = APIRouter(
    prefix='/profiles', 
    tags=['Profile Management'],
    dependencies=[Depends(get_current_user)]
)

@router.post('/create', response_model=StandardResponse[ProfileResponse])
async def create_profile(profile: ProfileCreate, db: AsyncSession = Depends(get_db), admin: dict = Depends(check_admin_privilege)):
    provider_check = await db.get(ModelProvider, profile.provider_id)
    if not provider_check:
        raise ParameterException(constants.ERR_PROVIDER_NOT_FOUND)
    
    name_check = await db.execute(select(Profile).where(Profile.name == profile.name))
    if name_check.scalars().first():
        raise ParameterException(constants.ERR_PROFILE_NAME_EXISTS)

    if profile.prompt_id:
        prompt_check = await db.get(PromptLibrary, profile.prompt_id)
        if not prompt_check:
            raise ParameterException(constants.ERR_PROMPT_NOT_FOUND)

    db_profile = Profile(**profile.model_dump())
    db.add(db_profile)
    await db.commit()
    await db.refresh(db_profile)
    return StandardResponse.success(data=db_profile, message=constants.MSG_PROFILE_CREATED)

@router.get('/list', response_model=StandardResponse[List[ProfileResponse]])
async def list_profiles(db: AsyncSession = Depends(get_db)) -> StandardResponse:
    result = await db.execute(select(Profile).options(joinedload(Profile.provider)))
    profiles = result.scalars().all()
    for p in profiles:
        if p.provider:
            p.provider_name = p.provider.name
    return StandardResponse.success(data=profiles)

@router.post('/activate')
async def activate_profile(profile_id: int, db: AsyncSession = Depends(get_db), admin: dict = Depends(check_admin_privilege)):
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise ResourceNotFoundException(constants.ERR_PROFILE_NOT_FOUND)
    
    if profile.provider_id is None or profile.provider_id == -1:
        raise ParameterException(constants.ERR_ACTIVATE_NO_PROVIDER)
    
    await db.execute(update(Profile).values(is_active=False))
    profile.is_active = True
    await db.commit()
    return StandardResponse.success(message=constants.MSG_PROFILE_ACTIVATED)

@router.post('/update', response_model=StandardResponse[ProfileResponse])
async def update_profile(profile_id: int, update_data: ProfileUpdate, db: AsyncSession = Depends(get_db), admin: dict = Depends(check_admin_privilege)):
    db_profile = await db.get(Profile, profile_id)
    if not db_profile:
        raise ResourceNotFoundException(constants.ERR_PROFILE_NOT_FOUND)
    
    if update_data.name:
        check = await db.execute(select(Profile).where(Profile.name == update_data.name, Profile.id != profile_id))
        if check.scalars().first():
            raise ParameterException(constants.ERR_PROFILE_NAME_EXISTS)

    if update_data.prompt_id:
        prompt_check = await db.get(PromptLibrary, update_data.prompt_id)
        if not prompt_check:
            raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)

    for field, value in update_data.model_dump(exclude_unset=True).items():
        setattr(db_profile, field, value)
    await db.commit()
    await db.refresh(db_profile)
    return StandardResponse.success(data=db_profile, message=constants.MSG_PROFILE_UPDATED)

@router.post('/delete')
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db), admin: dict = Depends(check_admin_privilege)):
    db_profile = await db.get(Profile, profile_id)
    if not db_profile:
        raise ResourceNotFoundException(constants.ERR_PROFILE_NOT_FOUND)

    count_stmt = select(func.count()).select_from(Profile)
    count = (await db.execute(count_stmt)).scalar()
    if count <= 1:
        raise ParameterException(constants.ERR_DELETE_LAST_PROFILE)

    if db_profile.is_active:
        raise ParameterException(constants.ERR_DELETE_ACTIVE_PROFILE)

    await db.delete(db_profile)
    await db.commit()
    return StandardResponse.success(message=constants.MSG_PROFILE_DELETED)
