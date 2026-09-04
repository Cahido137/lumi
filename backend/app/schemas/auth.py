from datetime import datetime

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    """注册请求"""

    username: str = Field(
        ..., min_length=1, max_length=20, pattern=r"^[a-zA-Z][a-zA-Z0-9_]{2,19}$", description="用户名"
    )
    password: str = Field(..., min_length=6, max_length=72, description="密码")
    nickname: str | None = Field(None, max_length=50, description="昵称")


class LoginRequest(BaseModel):
    """登录请求"""

    username: str = Field(..., min_length=1, max_length=20, description="用户名")
    password: str = Field(..., min_length=1, max_length=72, description="密码")


class UserResponse(BaseModel):
    """用户信息响应"""

    uid: int
    username: str
    nickname: str | None
    created_at: datetime = Field(..., alias="createdAt")


class TokenResponse(BaseModel):
    """登录或注册成功响应, 携带token"""

    access_token: str = Field(..., alias="accessToken")
    token_type: str = Field("bearer", description="令牌类型")
    user: UserResponse
