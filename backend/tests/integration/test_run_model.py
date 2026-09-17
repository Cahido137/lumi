"""集成测试: runs 表的数据库级保证(默认值、外键行为、列宽、时间戳、索引与注释)。

Note:
    本文件直接用 ORM 与原生 SQL 验证 schema 本身, 不经过 CRUD 层。
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from app.crud import approvals as approvals_crud
from app.crud import messages as messages_crud
from app.crud import sessions as sessions_crud
from app.crud import tool_executions as tool_executions_crud
from app.crud import users as users_crud
from app.db.models import Run
from app.db.session import SessionLocal
from app.schemas.enums import MessageRole, RunStatus
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError


async def create_session_with_message(username: str = "run_tester") -> tuple[str, str]:
    """创建一个用户、一个会话与一条用户消息。

    Args:
        username: 用户名, 每个用例必须不同以避免唯一约束冲突。

    Returns:
        tuple[str, str]: (会话ID, 用户消息ID)。
    """
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "运行会话", user.id)
        await db.commit()
        message = await messages_crud.add_message(db, session.id, MessageRole.USER, "你好")
        await db.commit()
        return session.id, message.id


async def create_run(session_id: str, thread_id: str = "thread-1", **overrides) -> str:
    """插入一条运行记录。

    Args:
        session_id: 所属会话ID。
        thread_id: 本轮运行的 LangGraph 线程ID。
        **overrides: 需要显式指定的其余列。

    Returns:
        str: 运行记录的主键。
    """
    async with SessionLocal() as db:
        run = Run(session_id=session_id, thread_id=thread_id, **overrides)
        db.add(run)
        await db.commit()
        return run.id


async def load_run(run_id: str) -> Run | None:
    """用全新会话重新读取运行记录, 确保看到的是数据库里的状态而不是内存对象。"""
    async with SessionLocal() as db:
        return await db.get(Run, run_id)


# ---------- 默认值与列往返 ----------


async def test_run_defaults():
    """只给必填列时, 其余列取模型默认值或保持为空"""
    session_id, _ = await create_session_with_message("run_defaults")
    run = await load_run(await create_run(session_id))
    assert run.status == RunStatus.PENDING
    assert run.attempt == 1
    assert (run.started_at, run.finished_at, run.cancel_requested_at) == (None, None, None)
    assert (run.error_code, run.input_message_id, run.lease_owner, run.lease_until) == (None, None, None, None)
    assert run.created_at is not None
    assert run.updated_at is not None


async def test_run_roundtrips_every_column():
    """全部列都能写入并原样读回"""
    session_id, message_id = await create_session_with_message("run_roundtrip")
    now = datetime.now(UTC)
    run = await load_run(
        await create_run(
            session_id,
            thread_id="thread-full",
            status=RunStatus.FAILED.value,
            input_message_id=message_id,
            attempt=3,
            error_code="run_in_progress",
            cancel_requested_at=now,
            lease_owner="worker-1",
            lease_until=now + timedelta(seconds=30),
            started_at=now,
            finished_at=now + timedelta(seconds=5),
        )
    )
    assert run.thread_id == "thread-full"
    assert run.status == RunStatus.FAILED
    assert run.input_message_id == message_id
    assert run.attempt == 3
    assert run.error_code == "run_in_progress"
    assert run.lease_owner == "worker-1"
    assert run.cancel_requested_at is not None
    assert run.lease_until is not None
    assert run.finished_at > run.started_at


# ---------- 外键行为: 审计账本 ----------


async def test_input_message_set_null_when_message_deleted():
    """重试删掉输入消息后运行记录仍在, 只把输入消息ID置空"""
    session_id, message_id = await create_session_with_message("run_set_null")
    run_id = await create_run(session_id, input_message_id=message_id)
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM messages WHERE id = :mid"), {"mid": message_id})
        await db.commit()
    run = await load_run(run_id)
    assert run is not None
    assert run.input_message_id is None


async def test_runs_cascade_when_session_deleted():
    """删除会话时运行记录级联删除"""
    session_id, _ = await create_session_with_message("run_cascade")
    run_id = await create_run(session_id)
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM sessions WHERE id = :sid"), {"sid": session_id})
        await db.commit()
    assert await load_run(run_id) is None


async def test_session_id_and_thread_id_are_required():
    """会话ID与线程ID都不可为空"""
    session_id, _ = await create_session_with_message("run_required")
    async with SessionLocal() as db:
        db.add(Run(session_id=None, thread_id="t-x"))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
    async with SessionLocal() as db:
        db.add(Run(session_id=session_id, thread_id=None))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


async def test_foreign_key_delete_rules():
    """删除行为在数据库侧就是 SET NULL 与 CASCADE, 不只依赖 ORM 声明"""
    async with SessionLocal() as db:
        rows = await db.execute(
            text(
                "SELECT conname, confdeltype::text FROM pg_constraint "
                "WHERE conrelid = 'runs'::regclass AND contype = 'f'"
            )
        )
    rules = dict(rows.all())
    assert rules["runs_session_id_fkey"] == "c"
    assert rules["runs_input_message_id_fkey"] == "n"


# ---------- 列宽与状态取值 ----------


async def test_status_column_fits_every_enum_value():
    """varchar(20) 装得下全部状态值, 最长的 waiting_approval 是 16 字符"""
    session_id, _ = await create_session_with_message("run_status_width")
    for status in RunStatus:
        run = await load_run(await create_run(session_id, thread_id=f"thread-{status.value}", status=status.value))
        assert run.status == status
    assert max(len(item.value) for item in RunStatus) == 16


async def test_status_column_rejects_overlong_value():
    """超出列宽的值被数据库拒绝, 说明列宽是真实约束而不是装饰"""
    session_id, _ = await create_session_with_message("run_status_overflow")
    async with SessionLocal() as db:
        db.add(Run(session_id=session_id, thread_id="t-long", status="x" * 21))
        # asyncpg 方言把字符串超长归到 DBAPIError(psycopg2 才是 DataError), 因此靠报文确认原因
        with pytest.raises(DBAPIError) as exc:
            await db.commit()
        await db.rollback()
    assert "value too long" in str(exc.value)


# ---------- 时间戳语义 ----------


async def test_updated_at_advances_on_status_change():
    """状态变更由 onupdate 推进 updated_at, created_at 保持不变"""
    session_id, _ = await create_session_with_message("run_timestamps")
    run_id = await create_run(session_id)
    created_at = (await load_run(run_id)).created_at
    await asyncio.sleep(0.01)
    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
        run.status = RunStatus.RUNNING.value
        await db.commit()
    after = await load_run(run_id)
    assert after.status == RunStatus.RUNNING
    assert after.created_at == created_at
    assert after.updated_at > created_at


async def test_started_at_is_independent_from_created_at():
    """登记时刻与取得执行权的时刻是两个独立列, 差值即排队时长"""
    session_id, _ = await create_session_with_message("run_queue_delay")
    run_id = await create_run(session_id)
    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
        run.started_at = run.created_at + timedelta(seconds=2)
        await db.commit()
    after = await load_run(run_id)
    assert after.started_at - after.created_at == timedelta(seconds=2)


# ---------- 与既有表的关系 ----------


async def test_thread_id_joins_with_approvals():
    """runs.thread_id 与 approvals.thread_id 同型同值, 可以直接 join"""
    session_id, _ = await create_session_with_message("run_join")
    thread_id = "thread-join-1"
    await create_run(session_id, thread_id=thread_id, status=RunStatus.WAITING_APPROVAL.value)
    async with SessionLocal() as db:
        execution = await tool_executions_crud.create_pending_execution(
            db, session_id, "run_shell", {"command": "ls"}, "call-1"
        )
        await approvals_crud.create_approval(db, session_id, thread_id, execution.id)
        await db.commit()
        rows = await db.execute(
            text("SELECT r.id, a.id FROM runs r JOIN approvals a ON a.thread_id = r.thread_id WHERE r.thread_id = :t"),
            {"t": thread_id},
        )
    assert len(rows.all()) == 1


# ---------- 迁移与模型的一致性 ----------


async def test_indexes_exist_in_database():
    """迁移建出的三个索引确实存在于库中"""
    async with SessionLocal() as db:
        rows = await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'runs'"))
    names = set(rows.scalars().all())
    assert {"ix_runs_session_id", "ix_runs_thread_id", "ix_runs_input_message_id"} <= names


async def test_column_comments_match_model():
    """库里的列注释与模型定义逐字一致, 改了模型却忘记重跑迁移就会红"""
    expected = {column.name: column.comment for column in Run.__table__.columns}
    async with SessionLocal() as db:
        rows = await db.execute(
            text(
                "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM pg_attribute a "
                "WHERE a.attrelid = 'runs'::regclass AND a.attnum > 0 AND NOT a.attisdropped"
            )
        )
    assert dict(rows.all()) == expected
