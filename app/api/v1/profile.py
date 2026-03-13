from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.providers.database import get_db
from app.models.profile import Profile
from app.models.provider import ModelProvider
from app.schemas.profile import ProfileCreate, ProfileResponse, ProfileUpdate
from app.core.security import get_current_user
from typing import List

router = APIRouter(
    prefix='/profiles', 
    tags=['Profile Management'],
    dependencies=[Depends(get_current_user)]
)

@router.post('/create', response_model=ProfileResponse)
async def create_profile(profile: ProfileCreate, db: AsyncSession = Depends(get_db)):
    # 校验关联的供应商是否存在
    provider_check = await db.get(ModelProvider, profile.provider_id)
    if not provider_check:
        raise HTTPException(status_code=400, detail=f'Provider with id {profile.provider_id} does not exist')
    
    # 校验名称唯一性
    name_check = await db.execute(select(Profile).where(Profile.name == profile.name))
    if name_check.scalars().first():
        raise HTTPException(status_code=400, detail=f'Profile name {profile.name} already exists')

    db_profile = Profile(**profile.model_dump())
    db.add(db_profile)
    await db.commit()
    await db.refresh(db_profile)
    return db_profile

@router.get('/list', response_model=List[ProfileResponse])
async def list_profiles(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Profile))
    return result.scalars().all()

@router.post('/activate')
async def activate_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    # 激活前校验 Profile 完整性（必须关联了有效的 Provider）
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail='Profile not found')
    
    if not profile.provider_id or profile.provider_id == 0:
        raise HTTPException(status_code=400, detail='Cannot activate profile without a valid provider')
    
    await db.execute(update(Profile).values(is_active=False))
    profile.is_active = True
    await db.commit()
    return {'message': f'Profile {profile.name} activated'}
@router.post('/update', response_model=ProfileResponse)
async def update_profile(profile_id: int, update_data: ProfileUpdate, db: AsyncSession = Depends(get_db)):
    db_profile = await db.get(Profile, profile_id)
    if not db_profile: raise HTTPException(status_code=404, detail='Profile not found')
    for field, value in update_data.model_dump(exclude_unset=True).items():
        setattr(db_profile, field, value)
    await db.commit()
    await db.refresh(db_profile)
    return db_profile

@router.post('/delete')
async def delete_profile(profile_id: int, db: AsyncSession = Depends(get_db)):
    # 1. 优先检查配置文件是否存在
    db_profile = await db.get(Profile, profile_id)
    if not db_profile: raise HTTPException(status_code=404, detail='Profile not found')

    # 2. 检查是否为最后一个配置文件
    from sqlalchemy import func
    count_stmt = select(func.count()).select_from(Profile)
    count = (await db.execute(count_stmt)).scalar()
    if count <= 1: raise HTTPException(status_code=400, detail='Cannot delete the last profile')

    # 3. 检查是否为激活状态
    if db_profile.is_active: raise HTTPException(status_code=400, detail='Cannot delete an active profile. Please activate another one first.')

    await db.delete(db_profile)
    await db.commit()
    return {'message': 'Profile deleted'}
