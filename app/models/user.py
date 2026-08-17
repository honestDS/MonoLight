from datetime import datetime

from pydantic import (
    ConfigDict,
    field_validator,
)
from pydantic import (
    Field as PydanticField,
)
from sqlmodel import (
    Column,
    DateTime,
    Field,
    SQLModel,
)

from app.core.utils.time import get_local_time


class UserBase(SQLModel):
    uid: str = Field(index=True, unique=True, nullable=False, max_length=50)
    username: str = Field(index=True, unique=True, nullable=False, max_length=50)
    is_active: bool = Field(default=True)
    is_superuser: bool = Field(default=False)


class User(UserBase, table=True):
    __tablename__ = "user"

    id: int | None = Field(default=None, primary_key=True)
    hashed_password: str | None = Field(default=None, max_length=255)

    created_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True)),
    )
    updated_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), onupdate=get_local_time),
    )


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

    @field_validator("username")
    @classmethod
    def validate_username_field(cls, value: str) -> str:
        from app.core.validation import validate_username

        return validate_username(value)

    @field_validator("password")
    @classmethod
    def validate_password_field(cls, value: str) -> str:
        from app.core.validation import validate_password

        return validate_password(value, require_non_empty=True)

    model_config = ConfigDict(json_schema_extra={"example": {"username": "new_user_01", "password": "secure_pass_123"}})


class UserUpdate(SQLModel):
    uid: str = PydanticField(..., description="用户UID", json_schema_extra={"example": "uuid_hex_string"})
    username: str | None = PydanticField(
        None,
        min_length=3,
        max_length=50,
        pattern=r"^[a-zA-Z0-9_\-]+$",
        json_schema_extra={"example": "updated_name"},
    )
    password: str | None = PydanticField(
        None,
        min_length=8,
        max_length=72,
        json_schema_extra={"example": "new_secure_password"},
    )
    is_active: bool | None = PydanticField(None, json_schema_extra={"example": True})

    @field_validator("username")
    @classmethod
    def validate_username_field(cls, value: str | None) -> str | None:
        if value is None:
            return None

        from app.core.validation import validate_username

        return validate_username(value)

    @field_validator("password")
    @classmethod
    def validate_password_field(cls, value: str | None) -> str | None:
        if value is None:
            return None

        from app.core.validation import validate_password

        return validate_password(value, require_non_empty=True)

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
