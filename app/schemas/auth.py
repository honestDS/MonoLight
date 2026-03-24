from pydantic import (
    BaseModel,
    Field,
    field_validator,
)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50, examples=["admin"])
    password: str = Field(..., min_length=1, max_length=72, examples=["password"])

    @field_validator("password")
    @classmethod
    def password_length_check(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError("password must not exceed 72 bytes")
        return v


class ResetAdminRequest(BaseModel):
    reset_token: str = Field(
        ..., min_length=32, max_length=32, examples=["ed126d6c5a4ea6bf33774214633d2a16"]
    )
