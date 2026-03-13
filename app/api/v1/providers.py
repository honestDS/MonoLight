from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.providers.database import get_db
from app.schemas.provider import ProviderCreate, ProviderRead, ProviderUpdate
from app.models.provider import ModelProvider
from app.core.security import get_current_user
from app.schemas.response import UnifiedResponse
from sqlalchemy import select
from typing import List, Any

router = APIRouter(prefix='/providers', tags=['Providers'], dependencies=[Depends(get_current_user)])



@router.post('/create', response_model=UnifiedResponse, status_code=status.HTTP_201_CREATED)
async def create_provider(provider_in: ProviderCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelProvider).where(ModelProvider.name == provider_in.name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='该提供商名称已存在')
    db_obj = ModelProvider(**provider_in.model_dump())
    db.add(db_obj)
    try:
        await db.commit()
        await db.refresh(db_obj)
        return UnifiedResponse.success(ProviderRead.model_validate(db_obj))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f'数据库提交失败: {str(e)}')

@router.get('/list', response_model=UnifiedResponse)
async def list_providers(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(ModelProvider))
        return UnifiedResponse.success([ProviderRead.model_validate(item) for item in result.scalars().all()])
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f'获取列表失败: {str(e)}')

@router.get('/get', response_model=UnifiedResponse)
async def get_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
    db_obj = result.scalar_one_or_none()
    if not db_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='未找到指定的提供商')
    return UnifiedResponse.success(ProviderRead.model_validate(db_obj))

@router.post('/update', response_model=UnifiedResponse)
async def update_provider(provider_id: int, provider_in: ProviderUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
    db_obj = result.scalar_one_or_none()
    if not db_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='未找到指定的提供商')
    
    update_data = provider_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_obj, field, value)
    
    try:
        await db.commit()
        await db.refresh(db_obj)
        return UnifiedResponse.success(ProviderRead.model_validate(db_obj))
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f'更新失败: {str(e)}')

@router.post('/delete')
async def delete_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
    db_obj = result.scalar_one_or_none()
    if not db_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='未找到指定的提供商')
    
    try:
        await db.delete(db_obj)
        await db.commit()
        return UnifiedResponse.success(message=f"成功删除模型提供商 ID: {provider_id}")
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f'删除失败: {str(e)}')