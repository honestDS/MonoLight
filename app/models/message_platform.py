from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, field_validator
from pydantic import Field as PydanticField
from sqlalchemy import String, UniqueConstraint, event
from sqlmodel import JSON, Column, DateTime, Field, SQLModel

from app.core.constants import DEFAULT_SESSION_GOAL_MODE, DEFAULT_SESSION_MAX_TURNS, SESSION_MAX_TURNS_UPPER_BOUND
from app.core.crypto import decrypt_api_key, encrypt_api_key
from app.core.i18n.locale import DEFAULT_LOCALE, SupportedLocale
from app.core.utils.time import get_local_time

ENCRYPTED_SECRET_PREFIX = "enc:v1:"


class MessagePlatformType(StrEnum):
    WEIXIN_OPENCLAW = "WEIXIN_OPENCLAW"


class MessagePlatformStatus(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    WAITING_LOGIN = "WAITING_LOGIN"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


class MessagePlatformSessionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable_markdown: StrictBool = False
    goal_mode: StrictBool = DEFAULT_SESSION_GOAL_MODE
    max_turns: StrictInt = PydanticField(default=DEFAULT_SESSION_MAX_TURNS, ge=1, le=SESSION_MAX_TURNS_UPPER_BOUND)
    show_tool_calls: StrictBool = True
    show_reasoning: StrictBool = True


class MessagePlatformConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_settings: MessagePlatformSessionSettings = PydanticField(default_factory=MessagePlatformSessionSettings)
    stream_llm_request: StrictBool = False


class MessagePlatformBase(SQLModel):
    name: str = Field(index=True, nullable=False, min_length=1, max_length=100)
    platform_type: MessagePlatformType = Field(nullable=False, index=True)
    language: SupportedLocale = Field(
        default=DEFAULT_LOCALE,
        min_length=1,
        max_length=20,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=DEFAULT_LOCALE,
        ),
    )
    is_enabled: bool = Field(default=False, index=True)
    use_stream_dispatch: bool = Field(default=False, nullable=False)
    status: MessagePlatformStatus = Field(default=MessagePlatformStatus.DISCONNECTED, nullable=False, index=True)
    account_id: str | None = Field(default=None, max_length=255)
    uid: str | None = Field(default=None, index=True, max_length=100)
    profile_id: int | None = Field(default=None, gt=0, index=True, foreign_key="profile.id", ondelete="RESTRICT")
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON), description="Platform private config such as secrets and runtime settings")
    state: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON), description="Platform runtime state such as sync_buf and qrcode status")
    last_error: str | None = Field(default=None, max_length=1000)


class MessagePlatform(MessagePlatformBase, table=True):
    __tablename__ = "message_platform"
    __table_args__ = (UniqueConstraint("uid", "name", name="uq_message_platform_uid_name"),)

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


def _decrypted_config(config: dict[str, Any] | None) -> dict[str, Any]:
    decrypted = dict(config or {})
    for key in ("token", "bot_token"):
        value = decrypted.get(key)
        if isinstance(value, str) and value and value.startswith(ENCRYPTED_SECRET_PREFIX):
            try:
                decrypted[key] = decrypt_api_key(value[len(ENCRYPTED_SECRET_PREFIX) :])
            except Exception:
                decrypted[key] = ""
    return decrypted


def _validate_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if config is None:
        return None
    return MessagePlatformConfig.model_validate(config).model_dump(exclude_unset=True)


@event.listens_for(MessagePlatform, "before_insert")
@event.listens_for(MessagePlatform, "before_update")
def encrypt_config_before_save(mapper, connection, target):
    # 仅为兼容老旧数据允许历史明文存储；新增或更新记录时，凭据必须加密后存储。
    target.config = _encrypt_config(target.config)


class MessagePlatformCreate(SQLModel):
    name: str = PydanticField(..., min_length=1, max_length=100)
    platform_type: MessagePlatformType = MessagePlatformType.WEIXIN_OPENCLAW
    language: SupportedLocale = DEFAULT_LOCALE
    is_enabled: bool = False
    use_stream_dispatch: bool = False
    uid: str | None = None
    profile_id: int | None = PydanticField(None, gt=0)
    account_id: str | None = None
    config: dict[str, Any] = PydanticField(default_factory=dict)
    state: dict[str, Any] = PydanticField(default_factory=dict)

    @field_validator("config", mode="before")
    @classmethod
    def validate_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_config(value)


class MessagePlatformUpdate(SQLModel):
    name: str | None = PydanticField(None, min_length=1, max_length=100)
    language: SupportedLocale | None = None
    is_enabled: bool | None = None
    use_stream_dispatch: bool | None = None
    uid: str | None = None
    profile_id: int | None = PydanticField(None, gt=0)
    account_id: str | None = None
    config: dict[str, Any] | None = None
    state: dict[str, Any] | None = None

    @field_validator("config", mode="before")
    @classmethod
    def validate_config(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_config(value)


class MessagePlatformResponse(BaseModel):
    id: int
    name: str
    platform_type: MessagePlatformType
    language: SupportedLocale
    is_enabled: bool
    use_stream_dispatch: bool
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
            data["config"] = _decrypted_config(obj.config)
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
