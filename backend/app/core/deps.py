"""鉴权与数据归属校验"""

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import users as users_crud
from app.crud import sessions as sessions_crud
from app.crud import approvals as approvals_crud
from app.db.models import User, Session, Approval
from app.db.session import get_db
from app.utils.security import decode_access_token


_bearer = HTTPBearer(auto_error=False)

async def get_current_user(
        request: Request, 
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
        db: AsyncSession = Depends(get_db)
) -> User:
    """从请求头的授权解析当前用户"""
    token = credentials.credentials if credentials else None  # 拿到用户token
    try:
        uid: int = decode_access_token(token)
    except (pyjwt.PyJWTError, AttributeError):
        # 如果解析不通过直接抛异常
        raise HTTPException(status_code=401, detail="未登录或登录信息失效")

    user = await users_crud.get_user_by_uid(db, uid)  # 根据uid查询用户
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user

async def get_owned_session_or_404(db: AsyncSession, session_id: str, user: User) -> Session:
    """会话归属"""
    session = await sessions_crud.get_session_for_user(db, session_id, user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session

async def get_owned_approval_or_404(db: AsyncSession, approval_id: str, user: User) -> Approval:
    """审批单归属"""
    approval = await approvals_crud.get_approval_by_id(db, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="审批单不存在")
    await get_owned_session_or_404(db, approval.session_id, user)  # 套用校验会话是否存在
    return approval