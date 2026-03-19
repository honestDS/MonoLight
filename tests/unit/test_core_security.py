import os
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from jose import jwt

from app.core.security import (
    UnifiedOAuth2PasswordBearer,
    create_access_token,
    get_current_user,
    get_password_hash,
    verify_password,
)

os.environ["JWT_SECRET_KEY"] = "test_secret"
os.environ["JWT_ALGORITHM"] = "HS256"
os.environ["ACCESS_TOKEN_EXPIRE_MINUTES"] = "60"

def test_password_hashing():
    password = "secret_password"
    hashed = get_password_hash(password)
    assert hashed != password
    assert verify_password(password, hashed) is True
    assert verify_password("wrong_password", hashed) is False

def test_verify_password_edge_cases():
    assert verify_password("", "hashed") is False
    assert verify_password("pwd", "") is False
    # 模拟异常路径 (31-32)
    with patch("bcrypt.checkpw", side_effect=Exception("error")):
        assert verify_password("pwd", "hashed") is False

def test_create_access_token():
    data = {"sub": "testuser"}
    token = create_access_token(data)
    assert isinstance(token, str)
    token_custom = create_access_token(data, expires_delta=timedelta(minutes=15))
    assert isinstance(token_custom, str)

@pytest.mark.asyncio
async def test_get_current_user_success():
    token = create_access_token({"sub": "admin"})
    mock_user = MagicMock()
    mock_user.username = "admin"
    
    mock_session = MagicMock()
    mock_session.execute = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = mock_user
    mock_session.execute.return_value = mock_result
    
    with patch("app.core.security.AsyncSessionLocal") as mock_factory:
        mock_factory.return_value.__aenter__.return_value = mock_session
        user_info = await get_current_user(token)
        assert user_info.username == "admin"

@pytest.mark.asyncio
async def test_get_current_user_not_found():
    token = create_access_token({"sub": "ghost"})
    mock_session = MagicMock()
    mock_session.execute = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_session.execute.return_value = mock_result
    
    with patch("app.core.security.AsyncSessionLocal") as mock_factory:
        mock_factory.return_value.__aenter__.return_value = mock_session
        with pytest.raises(HTTPException) as exc:
            await get_current_user(token)
        assert exc.value.status_code == 401

@pytest.mark.asyncio
async def test_get_current_user_invalid_token():
    with pytest.raises(HTTPException) as exc:
        await get_current_user("invalid.token")
    assert exc.value.status_code == 401

@pytest.mark.asyncio
async def test_get_current_user_no_sub():
    token = jwt.encode({"key": "value"}, os.environ["JWT_SECRET_KEY"], algorithm=os.environ["JWT_ALGORITHM"])
    with pytest.raises(HTTPException) as exc:
        await get_current_user(token)
    assert exc.value.status_code == 401

@pytest.mark.asyncio
async def test_unified_oauth2_password_bearer():
    bearer = UnifiedOAuth2PasswordBearer(tokenUrl="token")
    mock_request = MagicMock()
    mock_request.headers = {"Authorization": "Bearer test_token"}
    token = await bearer(mock_request)
    assert token == "test_token"
