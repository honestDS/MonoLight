import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.exceptions import (
    ForbiddenException,
    ParameterException,
    ResourceNotFoundException,
)
from app.core.security import get_current_user, get_password_hash
from app.models.user import User
from app.providers.database import get_db
from app.schemas.response import StandardResponse
from app.schemas.user import UserCreate, UserResponse, UserUpdate

router = APIRouter(
    prefix="/user", tags=["User Management"], dependencies=[Depends(get_current_user)]
)


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
    query = select(User).where(User.username == user_in.username)
    result = await db.execute(query)
    if result.scalars().first():
        raise ParameterException(constants.ERR_USER_NAME_EXISTS)

    try:
        generated_uid = uuid.uuid4().hex
        new_user = User(
            uid=generated_uid,
            username=user_in.username,
            hashed_password=get_password_hash(user_in.password)
            if user_in.password
            else None,
            is_superuser=user_in.is_superuser,
        )
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)
    except ValueError as e:
        raise ParameterException(str(e))

    return StandardResponse.success(
        data=UserResponse.model_validate(new_user), message=constants.MSG_USER_CREATED
    )


@router.get("/list")
async def list_all_users(
    db: AsyncSession = Depends(get_db), admin: dict = Depends(check_admin_privilege)
):
    result = await db.execute(select(User))
    users = result.scalars().all()
    return StandardResponse.success(
        data=[UserResponse.model_validate(u) for u in users],
        message=constants.MSG_USER_LIST_SUCCESS,
    )


@router.post("/update")
async def update_user(
    user_in: UserUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    query = select(User).where(User.uid == user_in.uid)
    result = await db.execute(query)
    user = result.scalars().first()

    if not user:
        raise ResourceNotFoundException(constants.ERR_USER_NOT_FOUND)

    if user.is_superuser:
        # 检查是否存在破坏性变更
        is_renaming = user_in.username and user_in.username != user.username
        is_deactivating = user_in.is_active is False
        is_demoting = user_in.is_superuser is False

        if is_renaming or is_deactivating or is_demoting:
            raise ParameterException(
                "超级管理员账户受核心保护，严禁执行禁用、降权或改名操作。"
            )
    if user_in.password:
        try:
            user.hashed_password = get_password_hash(user_in.password)
        except ValueError as e:
            raise ParameterException(str(e))

    if user_in.is_active is not None:
        user.is_active = user_in.is_active

    # 只有非超级管理员才允许修改用户名和身份（由管理员操作）
    if not user.is_superuser:
        if user_in.username:
            user.username = user_in.username
        if user_in.is_superuser is not None:
            user.is_superuser = user_in.is_superuser

    await db.commit()
    await db.refresh(user)
    return StandardResponse.success(
        data=UserResponse.model_validate(user), message=constants.MSG_USER_UPDATED
    )


@router.post("/delete")
async def delete_user(
    uid: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(check_admin_privilege),
):
    query = select(User).where(User.uid == uid)
    result = await db.execute(query)
    user = result.scalars().first()

    if not user:
        raise ResourceNotFoundException(constants.ERR_USER_NOT_FOUND)

    if user.is_superuser:
        raise ParameterException(
            "禁止删除超级管理员账户。如需注销，请先通过数据库手动降权或联系系统维护员。"
        )

    await db.delete(user)
    await db.commit()
    return StandardResponse.success(message=constants.MSG_USER_DELETED)
