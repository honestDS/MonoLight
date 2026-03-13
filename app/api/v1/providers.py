from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.providers.database import get_db
from app.schemas.provider import ProviderCreate, ProviderRead, ProviderUpdate
from app.models.provider import ModelProvider
from sqlalchemy import select
from typing import List

router = APIRouter(prefix='/providers', tags=['Providers'])

async def verify_admin_placeholder():
    # TODO: 实现真实的权限认证逻辑
    pass

@router.post('/create', response_model=ProviderRead, status_code=status.HTTP_201_CREATED, dependencies=[Depends(verify_admin_placeholder)])
async def create_provider(provider_in: ProviderCreate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelProvider).where(ModelProvider.name == provider_in.name))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='该提供商名称已存在')
    db_obj = ModelProvider(**provider_in.model_dump())
    db.add(db_obj)
    try:
        await db.commit()
        await db.refresh(db_obj)
        return db_obj
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f'数据库提交失败: {str(e)}')

@router.get('/list', response_model=List[ProviderRead])
async def list_providers(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(ModelProvider))
        return result.scalars().all()
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f'获取列表失败: {str(e)}')

@router.get('/get', response_model=ProviderRead)
async def get_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
    db_obj = result.scalar_one_or_none()
    if not db_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='未找到指定的提供商')
    return db_obj

@router.post('/update', response_model=ProviderRead, dependencies=[Depends(verify_admin_placeholder)])
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
        return db_obj
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f'更新失败: {str(e)}')

@router.post('/delete', dependencies=[Depends(verify_admin_placeholder)])
async def delete_provider(provider_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
    db_obj = result.scalar_one_or_none()
    if not db_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='未找到指定的提供商')
    
    try:
        await db.delete(db_obj)
        await db.commit()
        return {"message": f"成功删除模型提供商 ID: {provider_id}"}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f'删除失败: {str(e)}')