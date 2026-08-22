"""会话的 CRUD 操作"""

from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

# 导入会话 ORM
from app.db.models import Session


async def create_session(db: AsyncSession, title: str, user_id: str) -> Session:
    """创建一个会话"""
    session = Session(title=title, user_id=user_id)  # 创建会话 ORM 对象
    db.add(session)  # 向数据库中添加会话
    await db.flush()
    return session

async def list_sessions(db: AsyncSession, user_id: str,skip: int = 0, limit: int = 20) -> list[Session]:
    """按倒序返回会话列表"""
    # 按会话更新时间排序查询
    stmt = select(Session).where(Session.user_id == user_id).order_by(Session.updated_at.desc()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()

async def get_session_by_id(db: AsyncSession, session_id: str) -> Session | None:
    """按会话ID查询会话"""
    stmt = select(Session).where(Session.id == session_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()

async def get_session_for_user(db: AsyncSession, session_id: str, user_id: str) -> Session | None:
    """按会话ID和用户ID查询会话"""
    stmt = select(Session).where(Session.id == session_id, Session.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()

async def touch_session(db: AsyncSession, session_id: str) -> None:
    """刷新会话更新时间"""
    stmt = update(Session).where(Session.id == session_id).values(updated_at=func.now())
    await db.execute(stmt)
    await db.flush()