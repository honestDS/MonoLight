from pydantic import BaseModel, field_validator

class LoginRequest(BaseModel):
    username: str
    password: str

    @field_validator('password')
    @classmethod
    def password_length_check(cls, v: str) -> str:
        if len(v.encode('utf-8')) > 72:
            raise ValueError('密码长度不能超过 72 字节')
        return v

class TokenResponse(BaseModel):
    access_token: str
    token_type: str
