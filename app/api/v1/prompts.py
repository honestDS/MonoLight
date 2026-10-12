from fastapi import (
    APIRouter,
    Depends,
    Query,
)
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_ONLY_ADMIN_ALLOWED,
    ERR_PROMPT_IN_USE,
    ERR_PROMPT_NAME_EXISTS,
    ERR_PROMPT_NOT_FOUND,
    ERR_PROMPT_OWNER_CHANGE_IN_USE,
    ERR_PROMPT_REPLACEMENT_INVALID,
    ERR_SESSION_NO_PERMISSION,
    ERR_USER_NOT_FOUND,
    MSG_PROMPT_CREATED,
    MSG_PROMPT_DELETED,
    MSG_PROMPT_UPDATED,
)
from app.core.crud.account.user import user_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.profile.prompt import prompt_crud
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
        raise ForbiddenException(ERR_ONLY_ADMIN_ALLOWED)
    return current_user


@router.get("/list", response_model=StandardResponse)
async def list_prompts(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    skip = (page - 1) * size
    if getattr(current_user, "is_superuser", False):
        prompts = await prompt_crud.get_multi(db, skip=skip, limit=size)
        total = await prompt_crud.count(db)
    else:
        prompts = await prompt_crud.get_multi_visible(db, skip=skip, limit=size, uid=current_user.uid)
        total = await prompt_crud.count_visible(db, uid=current_user.uid)

    uids = list(dict.fromkeys(prompt.uid for prompt in prompts if prompt.uid))
    users = await user_crud.get_multi_by_uids(db, uids)
    usernames = {user.uid: user.username for user in users}
    items = []
    for prompt in prompts:
        item = PromptResponse.model_validate(prompt)
        item.username = usernames.get(prompt.uid)
        items.append(item)

    page_data = PageData(
        items=items,
        total=total,
        page=page,
        size=size,
        meta={"current_uid": current_user.uid},
    )
    return StandardResponse.success(data=page_data)


@router.post("/create", response_model=StandardResponse)
async def create_prompt(
    data: PromptCreate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    is_superuser = getattr(current_user, "is_superuser", False)
    if not is_superuser and data.uid is not None and data.uid != current_user.uid:
        raise ForbiddenException(ERR_SESSION_NO_PERMISSION)

    owner_uid = data.uid if is_superuser and data.uid is not None else current_user.uid
    owner = await user_crud.get_by_uid(db, owner_uid, lock_for_reference_write=True)
    if not owner:
        raise ResourceNotFoundException(ERR_USER_NOT_FOUND)
    owner_username = owner.username
    data.uid = owner.uid

    if await prompt_crud.get_by_name(db, data.name, uid=owner.uid):
        raise ParameterException(ERR_PROMPT_NAME_EXISTS)

    db_prompt = await prompt_crud.create(db, obj_in=data)
    response = PromptResponse.model_validate(db_prompt)
    response.username = owner_username
    return StandardResponse.success(
        data=response,
        message=MSG_PROMPT_CREATED,
    )


@router.post("/update", response_model=StandardResponse)
async def update_prompt(
    prompt_id: int,
    data: PromptUpdate,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        is_superuser = getattr(current_user, "is_superuser", False)
        owner = None
        if "uid" in data.model_fields_set and data.uid is not None:
            if not is_superuser and data.uid != current_user.uid:
                raise ForbiddenException(ERR_SESSION_NO_PERMISSION)
            owner = await user_crud.get_by_uid(db, data.uid, lock_for_reference_write=True)

        db_prompt = await prompt_crud.get(db, prompt_id, lock_for_reference_write=True)
        if not db_prompt:
            raise ResourceNotFoundException(ERR_PROMPT_NOT_FOUND)

        if db_prompt.uid != current_user.uid and not is_superuser:
            raise ForbiddenException(ERR_SESSION_NO_PERMISSION)

        owner_uid = db_prompt.uid
        if "uid" in data.model_fields_set:
            if data.uid is None:
                raise ResourceNotFoundException(ERR_USER_NOT_FOUND)
            if not is_superuser and data.uid != db_prompt.uid:
                raise ForbiddenException(ERR_SESSION_NO_PERMISSION)
            owner_uid = data.uid
            if not owner:
                raise ResourceNotFoundException(ERR_USER_NOT_FOUND)
        elif owner_uid is not None:
            owner = await user_crud.get_by_uid(db, owner_uid)

        owner_changed = owner_uid != db_prompt.uid
        if owner_changed and await profile_crud.get_multi_by_prompt_id(db, prompt_id):
            raise ParameterException(ERR_PROMPT_OWNER_CHANGE_IN_USE, code=409)

        updated_name = data.name if data.name is not None else db_prompt.name
        if updated_name != db_prompt.name or owner_changed:
            existing_prompt = await prompt_crud.get_by_name(db, updated_name, uid=owner_uid)
            if existing_prompt and existing_prompt.id != db_prompt.id:
                raise ParameterException(ERR_PROMPT_NAME_EXISTS)

        db_prompt = await prompt_crud.update(db, db_obj=db_prompt, obj_in=data, commit=False)
        response = PromptResponse.model_validate(db_prompt)
        response.username = owner.username if owner else None
        await db.commit()
        return StandardResponse.success(
            data=response,
            message=MSG_PROMPT_UPDATED,
        )
    except Exception:
        await db.rollback()
        raise


@router.post("/delete")
async def delete_prompt(
    prompt_id: int,
    replacement_prompt_id: int | None = Query(None, gt=0),
    confirm_reassign: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    try:
        prompt_ids = {prompt_id}
        if replacement_prompt_id is not None:
            prompt_ids.add(replacement_prompt_id)
        locked_prompts = {}
        for locked_prompt_id in sorted(prompt_ids):
            locked_prompts[locked_prompt_id] = await prompt_crud.get(
                db,
                locked_prompt_id,
                lock_for_reference_write=True,
            )

        db_prompt = locked_prompts[prompt_id]
        if not db_prompt:
            raise ResourceNotFoundException(ERR_PROMPT_NOT_FOUND)
        if db_prompt.uid != current_user.uid and not getattr(current_user, "is_superuser", False):
            raise ForbiddenException(ERR_SESSION_NO_PERMISSION)

        referenced_profiles = await profile_crud.get_multi_by_prompt_id(
            db,
            prompt_id,
            lock_for_reference_write=True,
        )
        if referenced_profiles:
            if not getattr(current_user, "is_superuser", False) and any(profile.uid != current_user.uid for profile in referenced_profiles):
                raise ForbiddenException(ERR_SESSION_NO_PERMISSION)
            if not confirm_reassign or not replacement_prompt_id:
                response = StandardResponse.error(
                    code=409,
                    message=ERR_PROMPT_IN_USE,
                )
                response.data = {"referenced_count": len(referenced_profiles)}
                await db.rollback()
                return JSONResponse(status_code=409, content=response.model_dump())
            if replacement_prompt_id == prompt_id:
                raise ParameterException(ERR_PROMPT_REPLACEMENT_INVALID)
            replacement_prompt = locked_prompts[replacement_prompt_id]
            if not replacement_prompt or replacement_prompt.uid != db_prompt.uid or any(profile.uid != replacement_prompt.uid for profile in referenced_profiles):
                raise ParameterException(ERR_PROMPT_REPLACEMENT_INVALID)
            await profile_crud.reassign_prompt(db, source_prompt_id=prompt_id, target_prompt_id=replacement_prompt_id)

        await prompt_crud.remove(db, id=prompt_id)
        return StandardResponse.success(message=MSG_PROMPT_DELETED)
    except Exception:
        await db.rollback()
        raise
