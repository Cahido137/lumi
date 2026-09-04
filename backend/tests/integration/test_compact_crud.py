"""集成测试: 上下文压缩数据层(消息usage落库, 边界消息查询, 会话摘要存取)"""

from app.crud import messages as messages_crud
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.session import SessionLocal
from app.schemas.enums import MessageRole
from app.schemas.usage import UsageMetadata


async def create_user_and_session(username="tester"):
    """创建用户与会话, 返回会话ID"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "新会话", user.id)
        await db.commit()
        return session.id


async def add_three_messages(session_id):
    """按顺序添加三条用户消息, 返回三个消息对象"""
    async with SessionLocal() as db:
        m1 = await messages_crud.add_message(db, session_id, MessageRole.USER, "a")
        m2 = await messages_crud.add_message(db, session_id, MessageRole.USER, "b")
        m3 = await messages_crud.add_message(db, session_id, MessageRole.USER, "c")
        await db.commit()
        return m1, m2, m3


# ---------- add_message: usage落库 ----------


async def test_add_message_stores_usage():
    """assistant消息的usage序列化后持久化到JSONB列"""
    sid = await create_user_and_session()
    usage = UsageMetadata(input_tokens=600, output_tokens=50, total_tokens=650)
    async with SessionLocal() as db:
        msg = await messages_crud.add_message(db, sid, MessageRole.ASSISTANT, "ok", usage=usage)
        await db.commit()
        msg_id = msg.id
    # 换一个全新数据库会话重新读取, 确认真正落库而不是会话内存对象
    async with SessionLocal() as db:
        saved = await messages_crud.get_message_by_id(db, msg_id)
        assert saved.usage == {"input_tokens": 600, "output_tokens": 50, "total_tokens": 650}


async def test_add_message_usage_defaults_to_none():
    """不传usage时列保持None, 不写入空字典"""
    sid = await create_user_and_session()
    async with SessionLocal() as db:
        msg = await messages_crud.add_message(db, sid, MessageRole.USER, "hi")
        await db.commit()
        msg_id = msg.id
    async with SessionLocal() as db:
        saved = await messages_crud.get_message_by_id(db, msg_id)
        assert saved.usage is None


async def test_add_message_usage_extra_fields_preserved():
    """缓存明细等供应商附加字段也随usage一并持久化"""
    sid = await create_user_and_session()
    usage = UsageMetadata.model_validate(
        {
            "input_tokens": 350,
            "output_tokens": 240,
            "total_tokens": 590,
            "input_token_details": {"cache_read": 100},
        }
    )
    async with SessionLocal() as db:
        msg = await messages_crud.add_message(db, sid, MessageRole.ASSISTANT, "ok", usage=usage)
        await db.commit()
        msg_id = msg.id
    async with SessionLocal() as db:
        saved = await messages_crud.get_message_by_id(db, msg_id)
        assert saved.usage["input_token_details"] == {"cache_read": 100}


# ---------- list_messages_after: 边界查询 ----------


async def test_list_messages_after_none_returns_all():
    """不指定边界时返回会话全量消息, 正序"""
    sid = await create_user_and_session()
    await add_three_messages(sid)
    async with SessionLocal() as db:
        msgs = await messages_crud.list_messages_after(db, sid, None)
        assert [m.content for m in msgs] == ["a", "b", "c"]


async def test_list_messages_after_boundary_exclusive():
    """返回严格在边界之后的消息(不含边界本身), 正序; 边界是最后一条时返回空"""
    sid = await create_user_and_session()
    m1, m2, m3 = await add_three_messages(sid)
    async with SessionLocal() as db:
        after_m1 = await messages_crud.list_messages_after(db, sid, m1.id)
        assert [m.content for m in after_m1] == ["b", "c"]
        after_m3 = await messages_crud.list_messages_after(db, sid, m3.id)
        assert after_m3 == []


async def test_list_messages_after_deleted_boundary_falls_back_to_all():
    """边界消息已被删除时退回全量查询而不是报错(重试场景的兜底)"""
    sid = await create_user_and_session()
    m1, m2, m3 = await add_three_messages(sid)
    m2_id = m2.id
    # 模拟重试删除了边界消息
    async with SessionLocal() as db:
        boundary = await messages_crud.get_message_by_id(db, m2_id)
        await db.delete(boundary)
        await db.commit()
    async with SessionLocal() as db:
        msgs = await messages_crud.list_messages_after(db, sid, m2_id)
        assert [m.content for m in msgs] == ["a", "c"]


async def test_list_messages_after_cross_session_falls_back_to_all():
    """边界消息属于其他会话时退回本会话全量查询, 不泄漏跨会话数据"""
    sid1 = await create_user_and_session("user1")
    sid2 = await create_user_and_session("user2")
    await add_three_messages(sid1)
    async with SessionLocal() as db:
        other = await messages_crud.add_message(db, sid2, MessageRole.USER, "x")
        await db.commit()
        other_id = other.id
    async with SessionLocal() as db:
        msgs = await messages_crud.list_messages_after(db, sid1, other_id)
        assert [m.content for m in msgs] == ["a", "b", "c"]


# ---------- set/get_context_summary: 摘要存取 ----------


async def test_context_summary_roundtrip():
    """摘要写入后读回一致"""
    sid = await create_user_and_session()
    _, _, m3 = await add_three_messages(sid)
    async with SessionLocal() as db:
        await sessions_crud.set_context_summary(db, sid, "这是摘要", m3.id)
        await db.commit()
    async with SessionLocal() as db:
        summary, until_id = await sessions_crud.get_context_summary(db, sid)
        assert summary == "这是摘要"
        assert until_id == m3.id


async def test_context_summary_clear():
    """两个参数传None可清空摘要"""
    sid = await create_user_and_session()
    _, _, m3 = await add_three_messages(sid)
    async with SessionLocal() as db:
        await sessions_crud.set_context_summary(db, sid, "这是摘要", m3.id)
        await sessions_crud.set_context_summary(db, sid, None, None)
        await db.commit()
    async with SessionLocal() as db:
        assert await sessions_crud.get_context_summary(db, sid) == (None, None)


async def test_context_summary_missing_session():
    """会话不存在时返回(None, None)而不报错"""
    async with SessionLocal() as db:
        assert await sessions_crud.get_context_summary(db, "00000000-0000-0000-0000-000000000000") == (None, None)
