from datetime import datetime
from typing import (
    Optional,
    List,
    TYPE_CHECKING,
)
from pydantic import (
    field_validator,
    ConfigDict,
    Field as PydanticField,
)
from sqlmodel import (
    Field,
    SQLModel,
    Relationship,
    Column,
    DateTime,
    func,
)

if TYPE_CHECKING:
    from app.models.prompt import PromptLibrary


class UserBase(SQLModel):
    uid: str = Field(index=True, unique=True, nullable=False, max_length=50)
    username: str = Field(index=True, unique=True, nullable=False, max_length=50)
    is_active: bool = Field(default=True)
    is_superuser: bool = Field(default=False)


class User(UserBase, table=True):
    __tablename__ = "user"

    id: Optional[int] = Field(default=None, primary_key=True)
    hashed_password: Optional[str] = Field(default=None, max_length=255)

    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), server_default=func.now()),
    )
    updated_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), onupdate=func.now())
    )

    prompts: List["PromptLibrary"] = Relationship(back_populates="user")


class UserCreate(SQLModel):
    username: str = PydanticField(
        ...,
        min_length=3,
        max_length=50,
        pattern=r"^[a-zA-Z0-9_\-]+$",
        description="用户名",
        json_schema_extra={"example": "new_user_01"},
    )
    password: str = PydanticField(
        ...,
        min_length=8,
        max_length=72,
        description="密码",
        json_schema_extra={"example": "secure_pass_123"},
    )

    @field_validator("password")
    @classmethod
    def password_length_check(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v.encode("utf-8")) > 72:
            raise ValueError("password must not exceed 72 bytes")
        return v

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"username": "new_user_01", "password": "secure_pass_123"}
        }
    )


class UserUpdate(SQLModel):
    uid: str = PydanticField(
        ..., description="用户UID", json_schema_extra={"example": "uuid_hex_string"}
    )
    username: Optional[str] = PydanticField(
        None,
        min_length=3,
        max_length=50,
        pattern=r"^[a-zA-Z0-9_\-]+$",
        json_schema_extra={"example": "updated_name"},
    )
    password: Optional[str] = PydanticField(
        None,
        min_length=8,
        max_length=72,
        json_schema_extra={"example": "new_secure_password"},
    )
    is_active: Optional[bool] = PydanticField(None, json_schema_extra={"example": True})

    @field_validator("password")
    @classmethod
    def password_length_check(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v.encode("utf-8")) > 72:
            raise ValueError("password must not exceed 72 bytes")
        return v

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "uid": "550e8400e29b41d4a716446655440000",
                "username": "updated_name",
                "password": "new_secure_password",
                "is_active": True,
            }
        }
    )


class UserResponse(UserBase):
    id: int
    model_config = ConfigDict(from_attributes=True)
