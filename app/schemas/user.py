from pydantic import BaseModel, Field, field_validator
from typing import Optional

class UserCreate(BaseModel):
    username: str = Field(..., description="用户名")
    password: Optional[str] = Field(None, description="密码")
    is_superuser: bool = Field(False, description="是否为超级管理员")

    @field_validator('password')
    @classmethod
    def password_length_check(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v.encode('utf-8')) > 72:
            raise ValueError('密码长度不能超过 72 字节')
        return v

class UserUpdate(BaseModel):
    uid: str = Field(..., description="用户UID")
    username: Optional[str] = Field(None, description="用户名")
    password: Optional[str] = Field(None, description="新密码")
    is_active: Optional[bool] = Field(None, description="是否激活")
    is_superuser: Optional[bool] = Field(None, description="是否为超级管理员")

    @field_validator('password')
    @classmethod
    def password_length_check(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v.encode('utf-8')) > 72:
            raise ValueError('密码长度不能超过 72 字节')
        return v

class UserResponse(BaseModel):
    id: int
    uid: str
    username: str
    is_active: bool
    is_superuser: bool

    class Config:
        from_attributes = True
