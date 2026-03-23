from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.core.utils.config import standardize_config


class ProviderConfig(BaseModel):
    model_id: str = Field(..., min_length=1, examples=["gemini-1.5-flash"])
    temperature: float = Field(0.7, ge=0, le=2.0)
    top_p: float = Field(1.0, ge=0, le=1.0)
    max_tokens: int = Field(2048, ge=0)
    stream: bool = Field(False)


class SecurityConfig(BaseModel):
    audit_provider_id: Optional[int] = Field(None, gt=0)
    audit_model_id: Optional[str] = Field(None)
    audit_threshold: int = Field(5, ge=0, le=7)


class ToolConfig(BaseModel):
    shell_timeout: float = Field(30.0, gt=0)


class OtherConfig(BaseModel):
    context_window_k: int = Field(4, ge=1)


class ProfileConfig(BaseModel):
    provider: ProviderConfig
    security: SecurityConfig
    tool: ToolConfig
    other: OtherConfig

    @model_validator(mode="before")
    @classmethod
    def data_pump(cls, data: Any) -> Any:
        # 定义当前模型的字段分布图
        schema_map = {
            "provider": ["model_id", "temperature", "top_p", "max_tokens", "stream"],
            "security": ["audit_provider_id", "audit_model_id", "audit_threshold"],
            "tool": ["shell_timeout"],
            "other": ["context_window_k"],
        }
        return standardize_config(data, schema_map)


class ProfileBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    provider_id: int = Field(..., ge=-1)
    prompt_id: Optional[int] = Field(None, gt=0)
    configs: ProfileConfig


class ProfileCreate(ProfileBase):
    pass


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    provider_id: Optional[int] = None
    prompt_id: Optional[int] = None
    configs: Optional[ProfileConfig] = None


class ProfileResponse(ProfileBase):
    id: int
    is_active: bool
    provider_name: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)
