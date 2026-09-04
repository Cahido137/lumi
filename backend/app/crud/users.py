"""用户的 CRUD 操作"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


async def create_user(db: AsyncSession, username: str, password_hash: str, nickname: str | None = None) -> User:
    """注册用户"""
    user = User(username=username, password_hash=password_hash, nickname=nickname or username)
    db.add(user)
    await db.flush()
    return user


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    """按内部ID查询用户"""
    return await db.get(User, user_id)


async def get_user_by_uid(db: AsyncSession, uid: int) -> User | None:
    """按uid查询用户"""
    stmt = select(User).where(User.uid == uid)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """按用户名查询用户"""
    stmt = select(User).where(User.username == username)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
