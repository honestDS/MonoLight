import os

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants
from app.core.exceptions import AuthException, ParameterException
from app.core.security import create_access_token, get_password_hash, verify_password
from app.models.user import User
from app.providers.database import get_db
from app.schemas.auth import LoginRequest
from app.schemas.response import StandardResponse

router = APIRouter()


@router.post("/login", response_model=StandardResponse)
async def login(request: LoginRequest = Body(...), db: AsyncSession = Depends(get_db)):
    # 移除环境变量直接登录逻辑，管理员需在数据库中真实存在

    query = select(User).where(User.username == request.username)
    result = await db.execute(query)
    user = result.scalars().first()

    if not user or not user.is_active:
        raise AuthException(constants.ERR_USER_NOT_FOUND_OR_DISABLED)

    try:
        if not user.hashed_password or not verify_password(
            request.password, user.hashed_password
        ):
            raise AuthException(constants.ERR_INVALID_CREDENTIALS)
    except ValueError as e:
        raise ParameterException(str(e))

    access_token = create_access_token(data={"sub": user.username})
    return StandardResponse.success(
        {"access_token": access_token, "token_type": "bearer"},
        message=constants.MSG_LOGIN_SUCCESS,
    )


class ResetAdminRequest(BaseModel):
    reset_token: str


@router.post("/reset_admin")
async def reset_admin_account(
    request: ResetAdminRequest = Body(...), db: AsyncSession = Depends(get_db)
):
    env_reset_token = os.getenv("ADMIN_RESET_TOKEN")
    if not env_reset_token or request.reset_token != env_reset_token:
        raise AuthException("无效或未配置重置 Token。")

    admin_username = os.getenv("ADMIN_USERNAME", "admin")

    # 查找或创建 admin 账户
    query = select(User).where(User.username == admin_username)
    result = await db.execute(query)
    user = result.scalars().first()

    new_hashed_password = get_password_hash("admin")

    if user:
        user.hashed_password = new_hashed_password
        user.is_superuser = True
        user.is_active = True
    else:
        import uuid

        user = User(
            uid=uuid.uuid4().hex,
            username=admin_username,
            hashed_password=new_hashed_password,
            is_superuser=True,
            is_active=True,
        )
        db.add(user)

    await db.commit()
    user_data = {
        "用户标识": user.uid,
        "登录账号": user.username,
        "初始密码": "admin",
        "账户状态": "已激活",
        "权限等级": "超级管理员",
    }
    return StandardResponse.success(
        data=user_data,
        message="超级管理员账户信息已成功重置，请及时登录并修改默认密码。",
    )
