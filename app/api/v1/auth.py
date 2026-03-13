import os
from fastapi import APIRouter, HTTPException, Depends, status
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.response import UnifiedResponse
from app.core.security import verify_password, create_access_token, get_password_hash

router = APIRouter()

@router.post('/login', response_model=UnifiedResponse)
async def login(request: LoginRequest):
    admin_user = os.getenv('ADMIN_USERNAME')
    if request.username != admin_user or request.password != os.getenv('ADMIN_PASSWORD'):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='用户名或密码错误',
        )
    access_token = create_access_token(data={'sub': admin_user})
    return UnifiedResponse.success({'access_token': access_token, 'token_type': 'bearer'})
