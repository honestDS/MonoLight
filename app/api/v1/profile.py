from app.core import messages
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import joinedload
from app.providers.database import get_db
from app.models.profile import Profile
from app.models.provider import ModelProvider
from app.models.prompt import PromptLibrary
from app.schemas.profile import ProfileCreate, ProfileResponse, ProfileUpdate
from app.schemas.response import UnifiedResponse
from app.core.security import get_current_user
from typing import List

router = APIRouter(
    prefix='/profiles', 
    tags=['Profile Management'],
    dependencies=[Depends(get_current_user)]
)

@router.post('/create', response_model=UnifiedResponse[ProfileResponse])
async def create_profile(profile: ProfileCreate, db: AsyncSession = Depends(get_db)):
    # 校验关联的供应商是否存在
    provider_check = await db.get(ModelProvider, profile.provider_id)
    if not provider_check:
        raise HTTPException(status_code=400, detail=messages.ERR_PROVIDER_NOT_FOUND)
    
    # 校验名称唯一性
    name_check = await db.execute(select(Profile).where(Profile.name == profile.name))
    if name_check.scalars().first():
        raise HTTPException(status_code=400, detail=messages.ERR_PROFILE_NAME_EXISTS)

    if profile.prompt_id:
        prompt_check = await db.get(PromptLibrary, profile.prompt_id)
        if not prompt_check:
            raise HTTPException(status_code=400, detail=messages.ERR_PROMPT_NOT_FOUND)

    db_profile = Profile(**profile.model_dump())
    db.add(db_profile)
    await db.commit()
    await db.refresh(db_profile)
    return UnifiedResponse.success(data=db_profile, message=messages.MSG_PROFILE_UPDATED)

@router.get('/list', response_model=UnifiedResponse[List[ProfileResponse]])
async def list_profiles(db: AsyncSession = Depends(get_db)) -> UnifiedResponse:
    result = await db.execute(select(Profile).options(joinedload(Profile.provider)))
    
    profiles = result.scalars().all()
    for p in profiles:
        if p.provider:
            p.provider_name = p.provider.name
    return UnifiedResponse.success(data=profiles)


@router.post('/activate')
async def activate_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    # 激活前校验 Profile 完整性
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail=messages.ERR_PROFILE_NOT_FOUND)
    
    if profile.provider_id is None or profile.provider_id == -1:
        raise HTTPException(status_code=400, detail=messages.ERR_ACTIVATE_NO_PROVIDER)
    
    await db.execute(update(Profile).values(is_active=False))
    profile.is_active = True
    await db.commit()
    return UnifiedResponse.success(message=messages.MSG_PROFILE_ACTIVATED)

@router.post('/update', response_model=UnifiedResponse[ProfileResponse])
async def update_profile(profile_id: int, update_data: ProfileUpdate, db: AsyncSession = Depends(get_db)):
    db_profile = await db.get(Profile, profile_id)
    if not db_profile: raise HTTPException(status_code=404, detail=messages.ERR_PROFILE_NOT_FOUND)
    
    # 名称唯一性检查
    if update_data.name:
        check = await db.execute(select(Profile).where(Profile.name == update_data.name, Profile.id != profile_id))
        if check.scalars().first():
            raise HTTPException(status_code=400, detail=messages.ERR_PROFILE_NAME_EXISTS)

    if update_data.prompt_id:
        prompt_check = await db.get(PromptLibrary, update_data.prompt_id)
        if not prompt_check:
            raise HTTPException(status_code=404, detail=messages.ERR_PROMPT_NOT_FOUND)

    for field, value in update_data.model_dump(exclude_unset=True).items():
        setattr(db_profile, field, value)
    await db.commit()
    await db.refresh(db_profile)
    return UnifiedResponse.success(data=db_profile, message=messages.MSG_PROFILE_UPDATED)

@router.post('/delete')
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    db_profile = await db.get(Profile, profile_id)
    if not db_profile: raise HTTPException(status_code=404, detail=messages.ERR_PROFILE_NOT_FOUND)

    from sqlalchemy import func
    count_stmt = select(func.count()).select_from(Profile)
    count = (await db.execute(count_stmt)).scalar()
    if count <= 1: raise HTTPException(status_code=400, detail=messages.ERR_DELETE_LAST_PROFILE)

    if db_profile.is_active: raise HTTPException(status_code=400, detail=messages.ERR_DELETE_ACTIVE_PROFILE)

    await db.delete(db_profile)
    await db.commit()
    return UnifiedResponse.success(message=messages.MSG_PROFILE_DELETED)
