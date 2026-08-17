from fastapi import (
    APIRouter,
    Body,
    Depends,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_INVALID_CREDENTIALS,
    ERR_PASSWORD_TOO_LONG_BYTES,
    ERR_USER_NOT_FOUND_OR_DISABLED,
    MSG_LOGIN_SUCCESS,
)
from app.core.crud.user import user_crud
from app.core.exceptions import (
    AuthException,
)
from app.core.security import (
    create_access_token,
    verify_password,
)
from app.core.validation import validate_password
from app.providers.database import get_db
from app.schemas.auth import (
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
