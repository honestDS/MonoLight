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