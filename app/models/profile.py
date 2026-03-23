from typing import Optional, Dict, Any, List, TYPE_CHECKING
from sqlmodel import SQLModel, Field, Relationship, Column, JSON, Integer, String, ForeignKey, Boolean
from pydantic import ConfigDict, BaseModel, model_validator
from app.core.utils.config import standardize_config

if TYPE_CHECKING:
    from app.models.provider import ModelProvider
    from app.models.prompt import PromptLibrary

class ProviderConfig(BaseModel):
    model_id: str = Field(..., min_length=1)
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
    max_parallel_tools: int = Field(5, ge=1, le=20)

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
        schema_map = {
            "provider": ["model_id", "temperature", "top_p", "max_tokens", "stream"],
            "security": ["audit_provider_id", "audit_model_id", "audit_threshold"],
            "tool": ["shell_timeout", "max_parallel_tools"],
            "other": ["context_window_k"],
        }
        return standardize_config(data, schema_map)

class ProfileBase(SQLModel):
    name: str = Field(index=True, unique=True, nullable=False, min_length=1, max_length=100)
    provider_id: int = Field(foreign_key="provider.id", ge=-1)
    prompt_id: Optional[int] = Field(default=None, foreign_key="prompt.id", gt=0)
    configs: Dict[str, Any] = Field(default={}, sa_column=Column(JSON))

class Profile(ProfileBase, table=True):
    __tablename__ = "profile"
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
    is_active: bool = Field(default=False)
    provider: "ModelProvider" = Relationship()
    prompt: Optional["PromptLibrary"] = Relationship(back_populates="profiles")

class ProfileCreate(ProfileBase):
    pass

class ProfileUpdate(SQLModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    provider_id: Optional[int] = Field(None, ge=-1)
    prompt_id: Optional[int] = Field(None, gt=0)
    configs: Optional[Dict[str, Any]] = None

class ProfileResponse(ProfileBase):
    id: int
    is_active: bool
    provider_name: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)
