"""会话的 CRUD 操作"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# 导入会话 ORM
from app.db.models import Session


async def create_session(db: AsyncSession, title: str):
    """创建一个会话"""
    session = Session(title=title)  # 创建会话 ORM 对象
    db.add(session)  # 向数据库中添加会话
    await db.flush()
    return session

async def list_sessions(db: AsyncSession, skip: int = 0, limit: int = 20):
    """按倒序返回会话列表"""
    # 按会话更新时间排序查询
    stmt = select(Session).order_by(Session.updated_at.desc()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()

async def get_session_by_id(db: AsyncSession, session_id: str):
    """按会话ID查询会话"""
    stmt = select(Session).where(Session.id == session_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()