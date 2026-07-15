import os
import uuid

from fastapi import (
    APIRouter,
    Body,
    Depends,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_AUTH_RESET_TOKEN_INVALID,
    ERR_INVALID_CREDENTIALS,
    ERR_PASSWORD_TOO_LONG_BYTES,
    ERR_USER_NOT_FOUND_OR_DISABLED,
    MSG_ADMIN_RESET_SUCCESS,
    MSG_LOGIN_SUCCESS,
)
from app.core.crud.user import user_crud
from app.core.exceptions import (
    AuthException,
)
from app.core.security import (
    create_access_token,
    get_password_hash,
    verify_password,
)
from app.models.user import UserCreate
from app.providers.database import get_db
from app.providers.database.bootstrap import ensure_default_profile_for_user
from app.schemas.auth import (
    LoginRequest,
    ResetAdminRequest,
)
from app.schemas.response import StandardResponse

router = APIRouter()


@router.post("/login", response_model=StandardResponse)
async def login(request: LoginRequest = Body(...), db: AsyncSession = Depends(get_db)):
    if len(request.password.encode("utf-8")) > 72:
        return StandardResponse.error(code=422, message=ERR_PASSWORD_TOO_LONG_BYTES)

    user = await user_crud.get_by_username(db, request.username)

    if not user or not user.is_active:
        raise AuthException(ERR_USER_NOT_FOUND_OR_DISABLED)

    if not user.hashed_password or not verify_password(request.password, user.hashed_password):
        raise AuthException(ERR_INVALID_CREDENTIALS)

    access_token = create_access_token(data={"sub": user.username})
    return StandardResponse.success(
        data={"access_token": access_token, "token_type": "bearer"},
        message=MSG_LOGIN_SUCCESS,
    )


@router.post("/reset_admin")
async def reset_admin_account(request: ResetAdminRequest = Body(...), db: AsyncSession = Depends(get_db)):
    env_reset_token = os.getenv("ADMIN_RESET_TOKEN")
    if not env_reset_token or request.reset_token != env_reset_token:
        raise AuthException(ERR_AUTH_RESET_TOKEN_INVALID)

    admin_username = os.getenv("ADMIN_USERNAME", "admin")
    user = await user_crud.get_by_username(db, admin_username)
    # Default password must meet the UserCreate validation requirement (min_length=8)
    default_password = "admin123"
    new_hashed_password = get_password_hash(default_password)

    if user:
        user = await user_crud.update(
            db,
            db_obj=user,
            obj_in={
                "hashed_password": new_hashed_password,
                "is_superuser": True,
                "is_active": True,
            },
        )
    else:
        user_in = UserCreate(username=admin_username, password=default_password)
        user = await user_crud.create(
            db,
            obj_in=user_in,
            update_dict={
                "uid": uuid.uuid4().hex,
                "hashed_password": new_hashed_password,
                "is_superuser": True,
                "is_active": True,
            },
        )

    await ensure_default_profile_for_user(db, user.uid)

    user_data = {
        "user_id": user.uid,
        "username": user.username,
        "initial_password": default_password,
        "account_status": "active",
        "role": "super_admin",
    }
    return StandardResponse.success(
        data=user_data,
        message=MSG_ADMIN_RESET_SUCCESS,
    )
