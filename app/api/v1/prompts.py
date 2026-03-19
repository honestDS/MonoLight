from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.security import get_current_user
from app.models.prompt import PromptLibrary
from app.providers.database import get_db
from app.schemas.prompt import PromptCreate, PromptResponse, PromptUpdate
from app.schemas.response import StandardResponse
from app.core import constants
from app.core.exceptions import ParameterException, ResourceNotFoundException


async def check_admin_privilege(current_user=Depends(get_current_user)):
    if not getattr(current_user, "is_superuser", False):
        from app.core import constants
        from app.core.exceptions import ForbiddenException

        raise ForbiddenException(constants.ERR_ONLY_ADMIN_ALLOWED)
    return current_user


router = APIRouter(
    prefix="/prompts",
    tags=["Prompt Management"],
    dependencies=[Depends(get_current_user)],
)


@router.get("/list", response_model=StandardResponse)
async def list_prompts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PromptLibrary))
    data = [PromptResponse.model_validate(p) for p in result.scalars().all()]
    return StandardResponse.success(data=data)


@router.post("/create", response_model=StandardResponse)
async def create_prompt(data: PromptCreate, db: AsyncSession = Depends(get_db)):
    check = await db.execute(
        select(PromptLibrary).where(PromptLibrary.name == data.name)
    )
    if check.scalars().first():
        raise ParameterException(constants.ERR_PROMPT_NAME_EXISTS)

    db_prompt = PromptLibrary(**data.model_dump())
    db.add(db_prompt)
    await db.commit()
    await db.refresh(db_prompt)
    return StandardResponse.success(
        data=PromptResponse.model_validate(db_prompt),
        message=constants.MSG_PROMPT_CREATED,
    )


@router.post("/update", response_model=StandardResponse)
async def update_prompt(
    prompt_id: int, data: PromptUpdate, db: AsyncSession = Depends(get_db)
):
    db_prompt = await db.get(PromptLibrary, prompt_id)
    if not db_prompt:
        raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)

    if data.name:
        check = await db.execute(
            select(PromptLibrary).where(
                PromptLibrary.name == data.name, PromptLibrary.id != prompt_id
            )
        )
        if check.scalars().first():
            raise ParameterException(constants.ERR_PROMPT_NAME_EXISTS)

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(db_prompt, field, value)
    await db.commit()
    await db.refresh(db_prompt)
    return StandardResponse.success(
        data=PromptResponse.model_validate(db_prompt),
        message=constants.MSG_PROMPT_UPDATED,
    )


@router.post("/delete")
async def delete_prompt(
    prompt_id: int,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_prompt = await db.get(PromptLibrary, prompt_id)
    if not db_prompt:
        raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)
    await db.delete(db_prompt)
    await db.commit()
    return StandardResponse.success(message=constants.MSG_PROMPT_DELETED)
