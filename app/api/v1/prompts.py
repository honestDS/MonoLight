from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.providers.database import get_db
from app.models.prompt import PromptLibrary
from app.schemas.prompt import PromptCreate, PromptResponse, PromptUpdate
from app.core.security import get_current_user
from typing import List

router = APIRouter(prefix='/prompts', tags=['Prompt Management'], dependencies=[Depends(get_current_user)])

@router.get('/list', response_model=List[PromptResponse])
async def list_prompts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PromptLibrary))
    return result.scalars().all()

@router.post('/create', response_model=PromptResponse)
async def create_prompt(data: PromptCreate, db: AsyncSession = Depends(get_db)):
    # 唯一性检查
    check = await db.execute(select(PromptLibrary).where(PromptLibrary.name == data.name))
    if check.scalars().first():
        raise HTTPException(status_code=400, detail=f'Prompt name {data.name} already exists')
        
    db_prompt = PromptLibrary(**data.model_dump())
    db.add(db_prompt)
    await db.commit()
    await db.refresh(db_prompt)
    return db_prompt

@router.post('/update', response_model=PromptResponse)
async def update_prompt(prompt_id: int, data: PromptUpdate, db: AsyncSession = Depends(get_db)):
    db_prompt = await db.get(PromptLibrary, prompt_id)
    if not db_prompt: raise HTTPException(status_code=404, detail='Prompt not found')
    
    if data.name:
        check = await db.execute(select(PromptLibrary).where(PromptLibrary.name == data.name, PromptLibrary.id != prompt_id))
        if check.scalars().first():
            raise HTTPException(status_code=400, detail=f'Prompt name {data.name} already exists')

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(db_prompt, field, value)
    await db.commit()
    await db.refresh(db_prompt)
    return db_prompt

@router.post('/delete')
async def delete_prompt(prompt_id: int, db: AsyncSession = Depends(get_db)):
    db_prompt = await db.get(PromptLibrary, prompt_id)
    if not db_prompt: raise HTTPException(status_code=404, detail='Prompt not found')
    await db.delete(db_prompt)
    await db.commit()
    return {'message': 'Prompt deleted'}
