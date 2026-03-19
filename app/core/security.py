from sqlalchemy import select
from app.models.user import User
from app.providers.database import AsyncSessionLocal
import os
import bcrypt
from datetime import datetime, timedelta, UTC
from jose import jwt


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        if isinstance(plain_password, str):
            password_bytes = plain_password.encode("utf-8")
        else:
            password_bytes = plain_password

        if isinstance(hashed_password, str):
            hash_bytes = hashed_password.encode("utf-8")
        else:
            hash_bytes = hashed_password

        return bcrypt.checkpw(password_bytes, hash_bytes)
    except Exception:
        return False


def get_password_hash(password: str) -> str:
    if isinstance(password, str):
        password_bytes = password.encode("utf-8")
    else:
        password_bytes = password

    if len(password_bytes) > 72:
        raise ValueError("密码长度不能超过 72 字节")

    # 生成盐值并哈希
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode("utf-8")


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(UTC) + timedelta(
        minutes=int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 10080))
    )
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(
        to_encode, os.getenv("JWT_SECRET_KEY"), algorithm=os.getenv("JWT_ALGORITHM")
    )
    return encoded_jwt


from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError


class UnifiedOAuth2PasswordBearer(OAuth2PasswordBearer):
    async def __call__(self, request: Request):
        try:
            return await super().__call__(request)
        except HTTPException as e:
            if e.status_code == 401:
                e.detail = "请先登录以获取访问权限"
            raise e


oauth2_scheme = UnifiedOAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的身份凭证",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token, os.getenv("JWT_SECRET_KEY"), algorithms=[os.getenv("JWT_ALGORITHM")]
        )
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception

        # 优化：在高频鉴权中复用连接池逻辑
        async with AsyncSessionLocal() as session:
            async with session.begin():
                stmt = select(User).where(User.username == username)
                result = await session.execute(stmt)
                user = result.scalars().first()
            if not user or not user.is_active:
                raise credentials_exception

            return {
                "uid": user.uid,
                "username": user.username,
                "is_superuser": user.is_superuser,
            }

    except JWTError:
        raise credentials_exception
