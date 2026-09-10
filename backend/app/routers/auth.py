"""用户认证相关路由。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.crud import users as users_crud
from app.db.models import User
from app.db.session import get_db
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.utils.response import success_response
from app.utils.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _build_token_response(user: User) -> TokenResponse:
    """构建注册、登录、查询共用的令牌响应体。

    Args:
        user: 用户对象。

    Returns:
        TokenResponse: 含访问令牌与用户公开信息的响应体。

    Note:
        令牌以 user.uid 为主体签发, 而不是使用内部主键 id。
    """
    return TokenResponse(
        accessToken=create_access_token(user.uid),
        user=UserResponse(uid=user.uid, username=user.username, nickname=user.nickname, createdAt=user.created_at),
    )


@router.post("/register")
async def register(request: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """注册新用户, 成功后直接返回令牌, 无需再次登录。

    Returns:
        JSONResponse: 三段式信封, data 载荷为 TokenResponse。

    Raises:
        HTTPException 400: 用户名或密码不符合格式约束, 或用户名已存在。
    """
    hashed_password = hash_password(request.password)
    user = await users_crud.create_user(db, request.username, hashed_password, request.nickname)
    return success_response(message="注册成功", data=_build_token_response(user))


@router.post("/login")
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    """用户登录, 成功后返回令牌。

    Returns:
        JSONResponse: 三段式信封, data 载荷为 TokenResponse。

    Raises:
        HTTPException 401: 用户名不存在或密码错误。
    """
    user = await users_crud.get_user_by_username(db, request.username)
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return success_response(message="登录成功", data=_build_token_response(user))


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    """获取当前登录用户的公开信息。

    Returns:
        JSONResponse: 三段式信封, data 载荷为 UserResponse。

    Raises:
        HTTPException 401: 未携带令牌或令牌无效。
    """
    return success_response(
        message="查询成功",
        data=UserResponse(
            uid=current_user.uid,
            username=current_user.username,
            nickname=current_user.nickname,
            createdAt=current_user.created_at,
        ),
    )
