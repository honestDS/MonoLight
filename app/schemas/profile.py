from typing import Any, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

class ProfileBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, examples=["test"])
    provider_id: int = Field(..., ge=-1, examples=[1])
    model_id: str = Field(..., min_length=1, examples=["gemini-3-flash-preview"])
    temperature: Optional[float] = Field(0.7, ge=0, le=2.0, examples=[0.7])
    top_p: Optional[float] = Field(1.0, ge=0, le=1.0, examples=[1.0])
    max_tokens: Optional[int] = Field(2048, examples=[0])
    stream: Optional[bool] = Field(False, examples=[False])
    extra_config: Optional[Dict[str, Any]] = Field(
        None, 
        examples=[{"shell_timeout": 30}]
    )
    context_window_k: Optional[int] = Field(4, ge=1, examples=[1024])
    prompt_id: Optional[int] = Field(None, gt=0, examples=[1])
    audit_provider_id: Optional[int] = Field(None, gt=0, examples=[0])
    audit_model_id: Optional[str] = Field(None, examples=["string"])
    audit_threshold: Optional[int] = Field(5, ge=0, le=7, examples=[5])

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
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    provider_id: Optional[int] = Field(None, ge=-1)
    prompt_id: Optional[int] = Field(None, gt=0)
    model_id: Optional[str] = Field(None, min_length=1)
    temperature: Optional[float] = Field(None, ge=0, le=2.0)
    top_p: Optional[float] = Field(None, ge=0, le=1.0)
    max_tokens: Optional[int] = None
    stream: Optional[bool] = None
    context_window_k: Optional[int] = Field(None, ge=1)
    audit_provider_id: Optional[int] = Field(None, gt=0)
    audit_model_id: Optional[str] = None
    audit_threshold: Optional[int] = Field(None, ge=0, le=7)
    extra_config: Optional[Dict[str, Any]] = None

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
