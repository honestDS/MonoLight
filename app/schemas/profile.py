from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProfileBase(BaseModel):
    name: str
    provider_id: int
    model_id: str
    temperature: Optional[float] = Field(0.7, ge=0, le=2.0)
    top_p: Optional[float] = Field(1.0, ge=0, le=1.0)
    max_tokens: Optional[int] = 2048
    stream: Optional[bool] = False
    extra_config: Optional[Dict[str, Any]] = None
    context_window_k: Optional[int] = Field(4, ge=1)
    prompt_id: Optional[int] = None

    @field_validator("extra_config")
    @classmethod
    def validate_shell_timeout(cls, v: Any) -> Any:
        if isinstance(v, dict) and "shell_timeout" in v:
            timeout = v["shell_timeout"]
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValueError("shell_timeout must be a positive number")
        return v


class ProfileCreate(ProfileBase):
    pass


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    provider_id: Optional[int] = None
    model_id: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0, le=2.0)
    top_p: Optional[float] = Field(None, ge=0, le=1.0)
    max_tokens: Optional[int] = None
    stream: Optional[bool] = None
    extra_config: Optional[Dict[str, Any]] = None
    context_window_k: Optional[int] = Field(None, ge=1)
    is_active: Optional[bool] = None
    prompt_id: Optional[int] = None

    @field_validator("extra_config")
    @classmethod
    def validate_shell_timeout(cls, v: Any) -> Any:
        if v is None:
            return v
        if isinstance(v, dict) and "shell_timeout" in v:
            timeout = v["shell_timeout"]
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                raise ValueError("shell_timeout must be a positive number")
        return v


class ProfileResponse(ProfileBase):
    id: int
    is_active: bool
    provider_name: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)
