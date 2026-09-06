"""认证接口的请求与响应模型。"""

from datetime import datetime

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    """用户注册请求体, 包含用户名、密码、昵称。

    用户名须以字母开头, 其后可为数字或下划线, 总长度不超过20。
    密码长度为 6-72 字符。
    """

    username: str = Field(
        ..., min_length=1, max_length=20, pattern=r"^[a-zA-Z][a-zA-Z0-9_]{2,19}$", description="用户名"
    )
    """用户名。以字母开头, 最大长度 20。"""

    password: str = Field(..., min_length=6, max_length=72, description="密码")
    """密码。长度限定为 6-72 字符。"""

    nickname: str | None = Field(None, max_length=50, description="昵称")
    """昵称。默认为空, 最大长度为 50。"""


class LoginRequest(BaseModel):
    """用户登录请求体。

    密码约束长度上限为 72 个字符。
    """

    username: str = Field(..., min_length=1, max_length=20, description="用户名")
    """用户名。"""

    password: str = Field(..., min_length=1, max_length=72, description="密码")
    """密码。"""


class UserResponse(BaseModel):
    """对外暴露的用户信息响应体。

    仅包含 uid、username、nickname、创建时间, 排除了密码哈希。
    """

    uid: int
    """用户对外标识。自增展示号。"""

    username: str
    """用户名。"""

    nickname: str | None
    """昵称。允许为空。"""

    created_at: datetime = Field(..., alias="createdAt")
    """用户注册时间。"""


class TokenResponse(BaseModel):
    """用户认证成功响应体, 携带访问令牌与用户信息。

    token_type 默认为 bearer, 用于构造 Authorization 请求头。
    user 为 UserResponse 类型。
    """

    access_token: str = Field(..., alias="accessToken")
    """访问令牌。"""

    token_type: str = Field("bearer", description="令牌类型")
    """令牌类型。默认为 bearer。"""

    user: UserResponse
    """用户信息。"""
