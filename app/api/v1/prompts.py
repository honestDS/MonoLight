from fastapi import (
    APIRouter,
    Depends,
    Query,
)
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.crud.profile import profile_crud
from app.core.crud.prompt import prompt_crud
from app.core.exceptions import (
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.security import get_current_user
from app.models.prompt import (
    PromptCreate,
    PromptResponse,
    PromptUpdate,
)
from app.providers.database import get_db
from app.schemas.response import (
    PageData,
    StandardResponse,
)

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
async def list_prompts(
    page: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    skip = (page - 1) * size
    prompts = await prompt_crud.get_multi_visible(db, skip=skip, limit=size, uid=current_user.uid)
    total = await prompt_crud.count_visible(db, uid=current_user.uid)

    page_data = PageData(
        items=[PromptResponse.model_validate(p) for p in prompts],
        total=total,
        page=page,
        size=size,
    )
    return StandardResponse.success(data=page_data)


@router.post("/create", response_model=StandardResponse)
async def create_prompt(
    data: PromptCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    data.uid = None if getattr(current_user, "is_superuser", False) and data.uid is None else current_user.uid
    if await prompt_crud.get_by_name(db, data.name, uid=data.uid):
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
    current_user=Depends(get_current_user),
):
    db_prompt = await prompt_crud.get(db, prompt_id)
    if not db_prompt:
        raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)
    if db_prompt.uid != current_user.uid and not getattr(current_user, "is_superuser", False):
        raise ForbiddenException(constants.ERR_SESSION_NO_PERMISSION)

    if data.name and data.name != db_prompt.name:
        if await prompt_crud.get_by_name(db, data.name, uid=db_prompt.uid):
            raise ParameterException(constants.ERR_PROMPT_NAME_EXISTS)

    db_prompt = await prompt_crud.update(db, db_obj=db_prompt, obj_in=data)
    return StandardResponse.success(
        data=PromptResponse.model_validate(db_prompt),
        message=constants.MSG_PROMPT_UPDATED,
    )


@router.post("/delete")
async def delete_prompt(
    prompt_id: int,
    replacement_prompt_id: int | None = Query(None, gt=0),
    confirm_reassign: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    db_prompt = await prompt_crud.get(db, prompt_id)
    if not db_prompt:
        raise ResourceNotFoundException(constants.ERR_PROMPT_NOT_FOUND)
    if db_prompt.uid != current_user.uid and not getattr(current_user, "is_superuser", False):
        raise ForbiddenException(constants.ERR_SESSION_NO_PERMISSION)

    referenced_profiles = await profile_crud.get_multi_by_prompt_id(db, prompt_id)
    if referenced_profiles:
        if not getattr(current_user, "is_superuser", False):
            raise ParameterException(constants.ERR_PROMPT_IN_USE)
        if not confirm_reassign or not replacement_prompt_id:
            response = StandardResponse.error(
                code=409,
                message=constants.ERR_PROMPT_IN_USE,
            )
            response.data = {"referenced_count": len(referenced_profiles)}
            return JSONResponse(status_code=409, content=response.model_dump())
        if replacement_prompt_id == prompt_id:
            raise ParameterException(constants.ERR_PROMPT_REPLACEMENT_INVALID)
        replacement_prompt = await prompt_crud.get(db, replacement_prompt_id)
        if not replacement_prompt or replacement_prompt.uid is not None:
            raise ParameterException(constants.ERR_PROMPT_REPLACEMENT_INVALID)
        await profile_crud.reassign_prompt(db, source_prompt_id=prompt_id, target_prompt_id=replacement_prompt_id)

    await prompt_crud.remove(db, id=prompt_id)
    return StandardResponse.success(message=constants.MSG_PROMPT_DELETED)
