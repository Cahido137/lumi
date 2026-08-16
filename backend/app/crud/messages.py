"""消息数据库操作"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# 导入消息 ORM
from app.db.models import Message


async def list_messages(db: AsyncSession, session_id: str, skip: int = 0, limit: int = 20):
    """查询指定会话分页消息"""
    stmt = select(Message).where(Message.session_id == session_id).order_by(Message.created_at.desc()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()

async def list_message_asc(db: AsyncSession, session_id: str):
    """查询指定会话所有消息, 正序排序"""
    stmt = select(Message).where(Message.session_id == session_id).order_by(Message.created_at.asc())
    result = await db.execute(stmt)
    return result.scalars().all()

async def add_message(db: AsyncSession, session_id: str, role: str, content: str) -> Message:
    """新增消息并返回消息对象"""
    message = Message(session_id=session_id, role=role, content=content)  # 创建消息 ORM
    db.add(message)  # 向数据库添加消息
    await db.flush()
    return message