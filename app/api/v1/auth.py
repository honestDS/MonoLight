from fastapi import (
    APIRouter,
    Body,
    Depends,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_CURRENT_PASSWORD_WRONG,
    ERR_INVALID_CREDENTIALS,
    ERR_PASSWORD_TOO_LONG_BYTES,
    ERR_UNAUTHORIZED,
    ERR_USER_NOT_FOUND_OR_DISABLED,
    MSG_LOGIN_SUCCESS,
    MSG_PASSWORD_CHANGED,
)
from app.core.crud.account.user import user_crud
from app.core.exceptions import (
    AuthException,
    ParameterException,
)
from app.core.security import (
    create_access_token,
    get_current_user,
    get_password_hash,
    verify_password,
)
from app.core.validation import validate_password
from app.models.user import (
    User,
    UserResponse,
)
from app.providers.database import get_db
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
)
from app.schemas.response import StandardResponse

router = APIRouter()


@router.post("/login", response_model=StandardResponse)
async def login(request: LoginRequest = Body(...), db: AsyncSession = Depends(get_db)):
    try:
        validate_password(request.password, require_non_empty=True, minimum_length=1)
    except ValueError:
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


@router.post("/change-password", response_model=StandardResponse)
async def change_password(
    request: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user = await user_crud.get_by_uid(
        db,
        current_user.uid,
        lock_for_reference_write=True,
    )

    if not user or not user.is_active:
        raise AuthException(ERR_UNAUTHORIZED)

    if not user.hashed_password or not verify_password(
        request.current_password,
        user.hashed_password,
    ):
        raise ParameterException(ERR_CURRENT_PASSWORD_WRONG)

    await user_crud.update(
        db,
        db_obj=user,
        obj_in={"hashed_password": get_password_hash(request.new_password)},
    )
    return StandardResponse.success(message=MSG_PASSWORD_CHANGED)


@router.get("/me", response_model=StandardResponse[UserResponse])
async def get_me(current_user: User = Depends(get_current_user)):
    return StandardResponse.success(data=UserResponse.model_validate(current_user))
