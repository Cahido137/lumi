"""会话表的 CRUD 操作。

Note:
    本模块的所有写操作均只 flush 不 commit, 事务边界由调用方决定。
    除 touch_session 显式写入 clock_timestamp() 外, 其余的 updated_at 由 onupdate 自动维护。
"""

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

# 导入会话 ORM
from app.db.models import Session


async def create_session(db: AsyncSession, title: str, user_id: str) -> Session:
    """创建一个新会话。

    Args:
        title: 会话标题。
        user_id: 会话所属的用户ID。

    Returns:
        Session: 成功创建的会话对象。
    """
    session = Session(title=title, user_id=user_id)  # 创建会话 ORM 对象
    db.add(session)  # 向数据库中添加会话
    await db.flush()
    return session


async def list_sessions(db: AsyncSession, user_id: str, skip: int = 0, limit: int = 20) -> list[Session]:
    """按时间倒序分页查询指定用户名下的会话列表。

    Args:
        user_id: 指定用户ID。
        skip: 跳过的条数。
        limit: 单页条数, 默认为 20。

    Returns:
        指定用户名下的指定分页会话列表。
    """
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
    """按会话主键ID查询会话。

    Args:
        session_id: 会话ID。

    Returns:
        如果存在指定会话, 返回其 Session 对象; 如果不存在, 返回 None。
    """
    stmt = select(Session).where(Session.id == session_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_session_for_user(db: AsyncSession, session_id: str, user_id: str) -> Session | None:
    """按会话主键ID查询会话, 同时校验其是否归属于指定用户。

    Args:
        session_id: 指定会话ID。
        user_id: 指定用户ID。

    Returns:
        如果存在指定会话, 返回其 Session 对象; 如果不存在, 返回 None。
    """
    stmt = select(Session).where(Session.id == session_id, Session.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def touch_session(db: AsyncSession, session_id: str) -> None:
    """刷新会话更新时间。

    Args:
        session_id: 需要刷新更新时间的会话ID。

    Note:
        此函数用于在对会话做出修改后手动更新其 updated_at 时间戳为此函数执行时刻。
    """
    stmt = update(Session).where(Session.id == session_id).values(updated_at=text("clock_timestamp()"))
    await db.execute(stmt)
    await db.flush()


async def set_has_pending_task(db: AsyncSession, session_id: str, value: bool) -> None:
    """设置会话是否存在被打断而未完成的任务。

    Args:
        session_id: 会话ID。
        value: 设置值。
    """
    stmt = update(Session).where(Session.id == session_id).values(has_pending_task=value)
    await db.execute(stmt)
    await db.flush()


async def get_has_pending_task(db: AsyncSession, session_id: str) -> bool:
    """查询会话是否存在被打断而未完成的任务。

    Args:
        session_id: 会话ID。

    Returns:
        bool: 是否存在被打断而未完成的任务。
    """
    stmt = select(Session.has_pending_task).where(Session.id == session_id)
    result = await db.execute(stmt)
    return bool(result.scalar_one_or_none())


async def set_context_summary(
    db: AsyncSession, session_id: str, summary_text: str | None, until_message_id: str | None
) -> None:
    """为指定会话保存压缩后上下文摘要。

    Args:
        session_id: 指定会话ID。
        summary_text: 压缩后的上下文摘要。
        until_message_id: 摘要所覆盖的最后一条消息ID。
    """
    stmt = (
        update(Session)
        .where(Session.id == session_id)
        .values(summary_text=summary_text, summary_until_message_id=until_message_id)
    )
    await db.execute(stmt)
    await db.flush()


async def get_context_summary(db: AsyncSession, session_id: str) -> tuple[str | None, str | None]:
    """查询会话的上下文摘要信息。

    Args:
        session_id: 指定会话ID。

    Returns:
        (摘要正文, 摘要覆盖的最后一条消息ID)

    Note:
        会话不存在, 或会话尚未进行过上下文压缩时, 返回 (None, None)。
    """
    stmt = select(Session.summary_text, Session.summary_until_message_id).where(Session.id == session_id)
    result = await db.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None, None
    return row.summary_text, row.summary_until_message_id
