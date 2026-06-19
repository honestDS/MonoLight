from pydantic import (
    BaseModel,
    Field,
)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50, examples=["admin"])
    password: str = Field(..., min_length=1, max_length=72, examples=["password"])


class ResetAdminRequest(BaseModel):
    reset_token: str = Field(..., min_length=32, max_length=32, examples=["ed126d6c5a4ea6bf33774214633d2a16"])
