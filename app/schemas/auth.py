from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.validation import validate_password


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50, examples=["admin"])
    password: str = Field(..., min_length=1, max_length=72, examples=["password"])


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=72)
    new_password: str = Field(..., min_length=8, max_length=72)

    model_config = ConfigDict(extra="forbid")

    @field_validator("current_password")
    @classmethod
    def validate_current_password(cls, value: str) -> str:
        return validate_password(value, minimum_length=1, require_non_empty=True)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return validate_password(value, require_non_empty=True)
