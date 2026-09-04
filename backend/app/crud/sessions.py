"""会话的 CRUD 操作"""

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

# 导入会话 ORM
from app.db.models import Session


async def create_session(db: AsyncSession, title: str, user_id: str) -> Session:
    """创建一个会话"""
    session = Session(title=title, user_id=user_id)  # 创建会话 ORM 对象
    db.add(session)  # 向数据库中添加会话
    await db.flush()
    return session


async def list_sessions(db: AsyncSession, user_id: str, skip: int = 0, limit: int = 20) -> list[Session]:
    """按倒序返回会话列表"""
    # 按会话更新时间排序查询
    stmt = (
        select(Session)
        .where(Session.user_id == user_id)
        .order_by(Session.updated_at.desc(), Session.id.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


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
    stmt = update(Session).where(Session.id == session_id).values(updated_at=text("clock_timestamp()"))
    await db.execute(stmt)
    await db.flush()


async def set_has_pending_task(db: AsyncSession, session_id: str, value: bool) -> None:
    """设置会话是否存在被打断而未完成的任务"""
    stmt = update(Session).where(Session.id == session_id).values(has_pending_task=value)
    await db.execute(stmt)
    await db.flush()


async def get_has_pending_task(db: AsyncSession, session_id: str) -> bool:
    """查询会话是否存在被打断而未完成的任务"""
    stmt = select(Session.has_pending_task).where(Session.id == session_id)
    result = await db.execute(stmt)
    return bool(result.scalar_one_or_none())


async def set_context_summary(
    db: AsyncSession, session_id: str, summary_text: str | None, until_message_id: str | None
) -> None:
    """为指定会话保存压缩后上下文摘要"""
    stmt = (
        update(Session)
        .where(Session.id == session_id)
        .values(summary_text=summary_text, summary_until_message_id=until_message_id)
    )
    await db.execute(stmt)
    await db.flush()


async def get_context_summary(db: AsyncSession, session_id: str) -> tuple[str | None, str | None]:
    """
    返回会话的上下文摘要信息

    Returns:
        (摘要正文, 摘要覆盖的最后一条消息ID)
    """
    stmt = select(Session.summary_text, Session.summary_until_message_id).where(Session.id == session_id)
    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None, None
    return row.summary_text, row.summary_until_message_id
