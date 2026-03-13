from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from app.providers.database import get_db
from app.models.profile import Profile
from app.schemas.profile import ProfileCreate, ProfileResponse, ProfileUpdate
from typing import List

router = APIRouter(prefix='/profiles', tags=['Profile Management'])

@router.post('/create', response_model=ProfileResponse)
async def create_profile(profile: ProfileCreate, db: AsyncSession = Depends(get_db)):
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
    await db.execute(update(Profile).values(is_active=False))
    result = await db.execute(update(Profile).where(Profile.id == profile_id).values(is_active=True))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail='Profile not found')
    await db.commit()
    return {'message': 'Profile activated'}