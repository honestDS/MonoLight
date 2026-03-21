from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserCreate(BaseModel):
    username: str = Field(
        ...,
        min_length=3,
        max_length=50,
        pattern=r"^[a-zA-Z0-9_\-]+$",
        description="用户名",
    )
    password: Optional[str] = Field(
        None, min_length=8, max_length=72, description="密码"
    )

    @field_validator("password")
    @classmethod
    def password_length_check(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v.encode("utf-8")) > 72:
            raise ValueError("password must not exceed 72 bytes")
        return v


class UserUpdate(BaseModel):
    uid: str = Field(..., description="用户UID")
    username: Optional[str] = Field(
        None, min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_\-]+$"
    )
    password: Optional[str] = Field(None, min_length=8, max_length=72)
    is_active: Optional[bool] = None

    @field_validator("password")
    @classmethod
    def password_length_check(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v.encode("utf-8")) > 72:
            raise ValueError("password must not exceed 72 bytes")
        return v


class UserResponse(BaseModel):
    id: int
    uid: str
    username: str
    is_active: bool
    is_superuser: bool
    model_config = ConfigDict(from_attributes=True)
