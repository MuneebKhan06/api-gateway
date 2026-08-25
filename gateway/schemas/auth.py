"""Request and response shapes for the auth endpoints."""

from pydantic import BaseModel, EmailStr, Field, field_validator

# Long enough to matter, short enough that bcrypt's 72 byte limit is not hit.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 72


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("password")
    @classmethod
    def reject_whitespace_only_padding(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("password cannot be only whitespace")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: str
    email: str
    roles: list[str]


class MessageResponse(BaseModel):
    message: str
