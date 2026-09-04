"""用户认证相关路由"""

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
    """构建 TokenResponse 响应"""
    return TokenResponse(
        accessToken=create_access_token(user.uid),
        user=UserResponse(uid=user.uid, username=user.username, nickname=user.nickname, createdAt=user.created_at),
    )


@router.post("/register")
async def register(request: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """注册"""
    hashed_password = hash_password(request.password)
    user = await users_crud.create_user(db, request.username, hashed_password, request.nickname)
    await db.commit()
    return success_response(message="注册成功", data=_build_token_response(user))


@router.post("/login")
async def login(request: LoginRequest, db: AsyncSession = Depends(get_db)):
    """登录"""
    user = await users_crud.get_user_by_username(db, request.username)
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="账号或密码错误")
    return success_response(message="登录成功", data=_build_token_response(user))


@router.get("/me")
async def me(current_user: User = Depends(get_current_user)):
    """获取当前用户登录信息"""
    return success_response(
        message="查询成功",
        data=UserResponse(
            uid=current_user.uid,
            username=current_user.username,
            nickname=current_user.nickname,
            createdAt=current_user.created_at,
        ),
    )
