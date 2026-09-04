"""消息数据库操作"""

from datetime import datetime

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import sessions as sessions_crud

# 导入消息 ORM
from app.db.models import Message
from app.schemas.enums import MessageRole
from app.schemas.usage import UsageMetadata


async def list_messages(db: AsyncSession, session_id: str, skip: int = 0, limit: int = 20):
    """查询指定会话分页消息"""
    stmt = (
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


async def list_message_asc(db: AsyncSession, session_id: str):
    """查询指定会话所有消息, 正序排序"""
    stmt = select(Message).where(Message.session_id == session_id).order_by(Message.created_at.asc(), Message.id.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


async def list_messages_after(db: AsyncSession, session_id: str, after_message_id: str | None = None) -> list[Message]:
    """查询指定会话的某条消息之后的所有消息(不包含指定消息)"""
    # 如果没有指定消息，则认为是全量查询
    if after_message_id is None:
        return await list_message_asc(db, session_id)
    # 获取指定消息，并查询之后的消息
    boundary = await db.get(Message, after_message_id)
    if boundary is None or boundary.session_id != session_id:
        return await list_message_asc(db, session_id)
    # 查询指定消息之后的消息
    stmt = (
        select(Message)
        .where(
            Message.session_id == session_id,
            or_(
                Message.created_at > boundary.created_at,
                and_(Message.created_at == boundary.created_at, Message.id > boundary.id),
            ),
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def add_message(
    db: AsyncSession,
    session_id: str,
    role: MessageRole | str,
    content: str,
    *,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    tool_calls: list | None = None,
    usage: UsageMetadata | None = None,
) -> Message:
    """新增消息并返回消息对象"""
    if isinstance(role, MessageRole):
        role = role.value
    message = Message(
        session_id=session_id,
        role=role,
        content=content,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_calls=tool_calls,
        usage=usage.model_dump() if usage is not None else None,
    )  # 创建消息 ORM
    db.add(message)  # 向数据库添加消息
    await sessions_crud.touch_session(db, session_id)
    await db.flush()
    return message


async def get_message_by_id(db: AsyncSession, message_id: str) -> Message | None:
    """按ID查询消息"""
    return await db.get(Message, message_id)


async def update_message_content(db: AsyncSession, message_id: str, content: str) -> None:
    """更新消息内容"""
    stmt = update(Message).where(Message.id == message_id).values(content=content).returning(Message.session_id)
    result = await db.execute(stmt)
    sid = result.scalar_one_or_none()
    if sid is not None:
        await sessions_crud.touch_session(db, sid)
    await db.flush()


async def has_user_message_after(db: AsyncSession, session_id: str, created_at: datetime) -> bool:
    """检查某个时间点后是否还有用户消息"""
    stmt = (
        select(Message.id)
        .where(
            Message.session_id == session_id, Message.role == MessageRole.USER.value, Message.created_at > created_at
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.first() is not None


async def delete_messages_after(db: AsyncSession, session_id: str, created_at: datetime) -> None:
    """删除指定会话中某个时间点之后的所有消息"""
    stmt = delete(Message).where(Message.session_id == session_id, Message.created_at > created_at)
    await db.execute(stmt)
    await sessions_crud.touch_session(db, session_id)
    await db.flush()


async def filter_existing_ids(db: AsyncSession, session_id: str, message_ids: list[str]) -> list[str]:
    """过滤在本会话中真实存在的id列表"""
    if not message_ids:
        return []
    stmt = select(Message.id).where(Message.session_id == session_id, Message.id.in_(message_ids))
    result = await db.execute(stmt)
    existing = set(result.scalars().all())
    return [mid for mid in message_ids if mid in existing]
