from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField
from sqlalchemy import event, inspect
from sqlmodel import JSON, Column, DateTime, Field, SQLModel

from app.core.crypto import decrypt_api_key, encrypt_api_key
from app.core.utils.time import get_local_time

ENCRYPTED_SECRET_PREFIX = "enc:v1:"


class MessagePlatformType(StrEnum):
    WEIXIN_OPENCLAW = "WEIXIN_OPENCLAW"


class MessagePlatformStatus(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    WAITING_LOGIN = "WAITING_LOGIN"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class MessagePlatformBase(SQLModel):
    name: str = Field(index=True, unique=True, nullable=False, min_length=1, max_length=100)
    platform_type: MessagePlatformType = Field(nullable=False, index=True)
    is_enabled: bool = Field(default=False, index=True)
    status: MessagePlatformStatus = Field(default=MessagePlatformStatus.DISCONNECTED, nullable=False, index=True)
    account_id: str | None = Field(default=None, max_length=255)
    uid: str | None = Field(default=None, index=True, max_length=100)
    profile_id: int | None = Field(default=None, gt=0, index=True)
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON), description="Platform private config such as secrets and runtime settings")
    state: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON), description="Platform runtime state such as sync_buf and qrcode status")
    last_error: str | None = Field(default=None, max_length=1000)


class MessagePlatform(MessagePlatformBase, table=True):
    __tablename__ = "message_platform"

    id: int | None = Field(default=None, primary_key=True, index=True)
    created_at: datetime | None = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True)))
    updated_at: datetime | None = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), onupdate=get_local_time))

    def get_config_secret(self, key: str, default: str = "") -> str:
        value = str((self.config or {}).get(key) or "")
        if not value:
            return default
        if value.startswith(ENCRYPTED_SECRET_PREFIX):
            return decrypt_api_key(value[len(ENCRYPTED_SECRET_PREFIX) :])
        return value


def _encrypt_secret(value: str) -> str:
    return f"{ENCRYPTED_SECRET_PREFIX}{encrypt_api_key(value)}"


def _encrypt_config(config: dict[str, Any] | None) -> dict[str, Any]:
    encrypted = dict(config or {})
    for key in ("token", "bot_token"):
        value = str(encrypted.get(key) or "").strip()
        if value and not value.startswith(ENCRYPTED_SECRET_PREFIX):
            encrypted[key] = _encrypt_secret(value)
    return encrypted


def _public_config(config: dict[str, Any] | None) -> dict[str, Any]:
    public = dict(config or {})
    for key in ("token", "bot_token"):
        public.pop(key, None)
    return public


@event.listens_for(MessagePlatform, "before_insert")
@event.listens_for(MessagePlatform, "before_update")
def encrypt_config_before_save(mapper, connection, target):
    state = inspect(target)
    history = state.get_history("config", passive=True)
    if history.unchanged:
        return
    target.config = _encrypt_config(target.config)


class MessagePlatformCreate(SQLModel):
    name: str = PydanticField(..., min_length=1, max_length=100)
    platform_type: MessagePlatformType = MessagePlatformType.WEIXIN_OPENCLAW
    is_enabled: bool = False
    uid: str | None = None
    profile_id: int | None = PydanticField(None, gt=0)
    account_id: str | None = None
    config: dict[str, Any] = PydanticField(default_factory=dict)
    state: dict[str, Any] = PydanticField(default_factory=dict)


class MessagePlatformUpdate(SQLModel):
    name: str | None = PydanticField(None, min_length=1, max_length=100)
    is_enabled: bool | None = None
    uid: str | None = None
    profile_id: int | None = PydanticField(None, gt=0)
    account_id: str | None = None
    config: dict[str, Any] | None = None
    state: dict[str, Any] | None = None


class MessagePlatformResponse(BaseModel):
    id: int
    name: str
    platform_type: MessagePlatformType
    is_enabled: bool
    status: MessagePlatformStatus
    account_id: str | None
    uid: str | None
    profile_id: int | None
    config: dict[str, Any]
    state: dict[str, Any]
    last_error: str | None
    created_at: datetime | None
    updated_at: datetime | None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def model_validate(cls, obj, **kwargs):
        if hasattr(obj, "config"):
            data = obj.model_dump()
            data["config"] = _public_config(obj.config)
            return super().model_validate(data, **kwargs)
        return super().model_validate(obj, **kwargs)


class WeixinOpenClawLoginStartResponse(BaseModel):
    platform_id: int
    qrcode: str
    qrcode_img_content: str
    status: MessagePlatformStatus


class WeixinOpenClawLoginStatusResponse(BaseModel):
    platform_id: int
    status: MessagePlatformStatus
    qrcode_status: str
    account_id: str | None = None
