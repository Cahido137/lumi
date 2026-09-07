"""消息表的 CRUD 操作。

Note:
    本模块的所有写操作均只 flush 不 commit, 事务边界由调用方决定。
    增、删、改消息时会刷新所属会话的 updated_at 时间戳。
"""

from datetime import datetime

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud import sessions as sessions_crud

# 导入消息 ORM
from app.db.models import Message
from app.schemas.enums import MessageRole
from app.schemas.usage import UsageMetadata


async def list_messages(db: AsyncSession, session_id: str, skip: int = 0, limit: int = 20):
    """分页查询指定会话的消息, 按时间倒序返回。

    Args:
        session_id: 会话ID。
        skip: 跳过的条数。
        limit: 单页条数, 默认为 20。

    Returns:
        最新的 limit 条消息, 按时间倒序返回。
    """
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
    """查询指定会话所有消息, 按时间正序返回。

    Args:
        session_id: 会话ID。

    Returns:
        本会话内所有消息列表, 按时间顺序返回。
    """
    stmt = select(Message).where(Message.session_id == session_id).order_by(Message.created_at.asc(), Message.id.asc())
    result = await db.execute(stmt)
    return result.scalars().all()


async def list_messages_after(db: AsyncSession, session_id: str, after_message_id: str | None = None) -> list[Message]:
    """查询指定会话的某条消息之后的所有消息, 不含该消息本身。

    Args:
        session_id: 会话ID。
        after_message_id: 边界消息ID, 可空。

    Returns:
        边界之后的消息列表, 按时间正序排列。

    Note:
        以下情况会静默降级为消息全量查询:
        边界ID为空、边界消息不存在、边界消息不属于本会话。
    """
    # 如果没有指定消息，则认为是全量查询
    if after_message_id is None:
        return await list_message_asc(db, session_id)
    # 取出边界消息。不存在或不属于本会话则降级为全量查询。
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
    """新增一条消息。

    Args:
        session_id: 所属会话ID。
        role: 消息角色, 接受 MessageRole 枚举或其字符串字面量。
        content: 消息正文, 无内容时传入空字符串。
        tool_call_id: 工具消息的调用标识, 仅消息类型为 tool 时传入。
        tool_name: 工具名称, 仅消息类型为 tool 时传入。
        tool_calls: 模型声明的工具调用列表, 仅消息类型为 assistant 时传入。
        usage: 模型返回的用量元数据。

    Returns:
        Message: 创建出的消息对象。

    Note:
        本函数会顺带刷新所属会话的更新时间。
    """
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
    """按主键ID查询消息。

    Args:
        message_id: 要查询的消息主键ID。

    Returns:
        如果消息存在, 返回指定消息的 Message 对象; 如果消息不存在, 返回 None。
    """
    return await db.get(Message, message_id)


async def update_message_content(db: AsyncSession, message_id: str, content: str) -> None:
    """改写指定消息的正文内容。

    Args:
        message_id: 消息ID。
        content: 新的正文内容。

    Note:
        指定消息不存在时会静默返回, 不会抛出异常。
    """
    stmt = update(Message).where(Message.id == message_id).values(content=content).returning(Message.session_id)
    result = await db.execute(stmt)
    sid = result.scalar_one_or_none()
    if sid is not None:
        await sessions_crud.touch_session(db, sid)
    await db.flush()


async def has_user_message_after(db: AsyncSession, session_id: str, created_at: datetime) -> bool:
    """检查某个时间点后是否还有用户消息。

    Args:
        session_id: 会话ID。
        created_at: 指定消息的创建时间戳。

    Returns:
        bool: 是否在指定时间点后还有用户消息。
    """
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
    """删除指定会话中某个时间点之后的所有消息。

    Args:
        session_id: 会话ID。
        created_at: 指定消息的创建时间戳。
    """
    stmt = delete(Message).where(Message.session_id == session_id, Message.created_at > created_at)
    await db.execute(stmt)
    await sessions_crud.touch_session(db, session_id)
    await db.flush()


async def filter_existing_ids(db: AsyncSession, session_id: str, message_ids: list[str]) -> list[str]:
    """过滤在本会话中真实存在的消息ID列表。

    Args:
        session_id: 会话ID。
        message_ids: 待过滤的消息ID列表。

    Returns:
        仅包含真实存在的ID列表, 并严格保证message_ids传入的顺序。
    """
    if not message_ids:
        return []
    stmt = select(Message.id).where(Message.session_id == session_id, Message.id.in_(message_ids))
    result = await db.execute(stmt)
    existing = set(result.scalars().all())
    return [mid for mid in message_ids if mid in existing]
