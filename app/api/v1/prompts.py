from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.core import constants
from app.core.exceptions import (
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.security import get_current_user
from app.models.prompt import PromptCreate, PromptResponse, PromptUpdate
from app.providers.database import get_db
from app.schemas.response import StandardResponse
from app.core.crud.prompt import prompt_crud

router = APIRouter(
    prefix="/prompts",
    tags=["Prompt Management"],
    dependencies=[Depends(get_current_user)],
)


async def check_admin_privilege(current_user=Depends(get_current_user)):
    if not getattr(current_user, "is_superuser", False):
        raise ForbiddenException(constants.ERR_ONLY_ADMIN_ALLOWED)
    return current_user


@router.get("/list", response_model=StandardResponse)
async def list_prompts(db: AsyncSession = Depends(get_db)):
    prompts = await prompt_crud.get_multi(db)
    return StandardResponse.success(
        data=[PromptResponse.model_validate(p) for p in prompts]
    )


@router.post("/create", response_model=StandardResponse)
async def create_prompt(
    data: PromptCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    if await prompt_crud.get_by_name(db, data.name):
        raise ParameterException(constants.ERR_PROMPT_NAME_EXISTS)

    db_prompt = await prompt_crud.create(db, obj_in=data)
    return StandardResponse.success(
        data=PromptResponse.model_validate(db_prompt),
        message=constants.MSG_PROMPT_CREATED,
    )


@router.post("/update", response_model=StandardResponse)
async def update_prompt(
    prompt_id: int,
    data: PromptUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    db_prompt = await prompt_crud.get(db, prompt_id)
    if not db_prompt:
        raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)

    if data.name and data.name != db_prompt.name:
        if await prompt_crud.get_by_name(db, data.name):
            raise ParameterException(constants.ERR_PROMPT_NAME_EXISTS)

    db_prompt = await prompt_crud.update(db, db_obj=db_prompt, obj_in=data)
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
    db_prompt = await prompt_crud.get(db, prompt_id)
    if not db_prompt:
        raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)
    await prompt_crud.remove(db, id=prompt_id)
    return StandardResponse.success(message=constants.MSG_PROMPT_DELETED)
