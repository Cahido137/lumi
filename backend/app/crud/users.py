"""用户表的 CRUD 操作。

Note:
    本模块的所有写操作均只 flush 不 commit, 事务边界由调用方决定。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


async def create_user(db: AsyncSession, username: str, password_hash: str, nickname: str | None = None) -> User:
    """创建一个用户。

    Args:
        username: 用户名。
        password_hash: 已哈希的密码。
        nickname: 昵称, 可空。为空时默认为用户名。

    Returns:
        User: 成功创建的用户对象。
    """
    user = User(username=username, password_hash=password_hash, nickname=nickname or username)
    db.add(user)
    await db.flush()
    return user


async def get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    """按主键ID查询用户。

    Args:
        user_id: 指定用户主键ID。

    Returns:
        如果存在指定用户, 返回其 User 对象; 如果不存在, 返回 None。
    """
    return await db.get(User, user_id)


async def get_user_by_uid(db: AsyncSession, uid: int) -> User | None:
    """按uid查询用户。

    Args:
        uid: 指定的用户uid。

    Returns:
        如果存在指定用户, 返回其 User 对象; 如果不存在, 返回 None。
    """
    stmt = select(User).where(User.uid == uid)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """按用户名查询用户。

    Args:
        username: 指定的用户名。

    Returns:
        如果存在指定用户, 返回其 User 对象; 如果不存在, 返回 None。
    """
    stmt = select(User).where(User.username == username)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
