import uuid

from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.crud.user import user_crud
from app.core.exceptions import (
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.security import (
    get_current_user,
    get_password_hash,
)
from app.models.user import (
    UserCreate,
    UserResponse,
    UserUpdate,
)
from app.providers.database import get_db
from app.schemas.response import (
    PageData,
    StandardResponse,
)

router = APIRouter(prefix="/user", tags=["User Management"], dependencies=[Depends(get_current_user)])


async def check_admin_privilege(current_user=Depends(get_current_user)):
    if not getattr(current_user, "is_superuser", False):
        raise ForbiddenException(constants.ERR_ONLY_ADMIN_ALLOWED)
    return current_user


@router.post("/add")
async def add_new_user(
    user_in: UserCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    if await user_crud.get_by_username(db, user_in.username):
        raise ParameterException(constants.ERR_USER_NAME_EXISTS)

    try:
        generated_uid = uuid.uuid4().hex
        new_user = await user_crud.create(
            db,
            obj_in=user_in,
            update_dict={
                "uid": generated_uid,
                "hashed_password": get_password_hash(user_in.password) if user_in.password else None,
                "is_superuser": False,
            },
        )
    except ValueError as e:
        raise ParameterException(str(e))

    return StandardResponse.success(data=UserResponse.model_validate(new_user), message=constants.MSG_USER_CREATED)


@router.get("/list")
async def list_all_users(
    page: int = 1,
    size: int = 10,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    skip = (page - 1) * size
    users = await user_crud.get_multi(db, skip=skip, limit=size)
    total = await user_crud.count(db)

    page_data = PageData(
        items=[UserResponse.model_validate(u) for u in users],
        total=total,
        page=page,
        size=size,
    )
    return StandardResponse.success(
        data=page_data,
        message=constants.MSG_USER_LIST_SUCCESS,
    )


@router.post("/update")
async def update_user(
    user_in: UserUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    user = await user_crud.get_by_uid(db, user_in.uid)
    if not user:
        raise ResourceNotFoundException(constants.ERR_USER_NOT_FOUND)

    if user.is_superuser:
        is_renaming = user_in.username and user_in.username != user.username
        is_deactivating = user_in.is_active is False
        if is_renaming or is_deactivating:
            raise ParameterException("超级管理员账户受核心保护，严禁执行禁用或改名操作。")

    update_dict = {}
    if user_in.password:
        try:
            update_dict["hashed_password"] = get_password_hash(user_in.password)
        except ValueError as e:
            raise ParameterException(str(e))

    if user_in.is_active is not None:
        update_dict["is_active"] = user_in.is_active

    if not user.is_superuser and user_in.username:
        update_dict["username"] = user_in.username

    user = await user_crud.update(db, db_obj=user, obj_in=update_dict)
    return StandardResponse.success(data=UserResponse.model_validate(user), message=constants.MSG_USER_UPDATED)


@router.post("/delete")
async def delete_user(
    uid: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    user = await user_crud.get_by_uid(db, uid)
    if not user:
        raise ResourceNotFoundException(constants.ERR_USER_NOT_FOUND)

    if user.is_superuser:
        raise ParameterException("禁止删除超级管理员账户。")

    await user_crud.remove(db, id=user.id)
    return StandardResponse.success(message=constants.MSG_USER_DELETED)
