"""集成测试: runs 表的数据库级保证(默认值、外键行为、列宽、时间戳、索引与注释)。

Note:
    本文件直接用 ORM 与原生 SQL 验证 schema 本身, 不经过 CRUD 层。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.crud import approvals as approvals_crud
from app.crud import messages as messages_crud
from app.crud import sessions as sessions_crud
from app.crud import tool_executions as tool_executions_crud
from app.crud import users as users_crud
from app.db.models import Approval, Run
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


async def test_output_message_set_null_when_message_deleted():
    """删除产出消息时运行记录保留, 外键置空"""
    session_id, message_id = await create_session_with_message("run_output_fk")
    run_id = await create_run(
        session_id, thread_id="thread-out", status=RunStatus.SUCCEEDED.value, output_message_id=message_id
    )
    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM messages WHERE id = :mid"), {"mid": message_id})
        await db.commit()
    run = await load_run(run_id)
    assert run is not None
    assert run.output_message_id is None


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
    for index, status in enumerate(RunStatus):
        # 每个状态各占一个会话: 同一会话同时只允许一条活动运行
        session_id, _ = await create_session_with_message(f"run_st_{index}")
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


# ---------- 准入约束: 状态取值与每会话一条活动运行 ----------


async def test_status_check_constraint_rejects_unknown_value():
    """状态取值由数据库 CHECK 兜底, 写入未知状态直接失败"""
    session_id, _ = await create_session_with_message("run_status_check")
    async with SessionLocal() as db:
        db.add(Run(session_id=session_id, thread_id="t-bad", status="unknown"))
        with pytest.raises(IntegrityError) as exc:
            await db.commit()
        await db.rollback()
    assert "ck_runs_status" in str(exc.value)


async def test_approval_status_check_constraint_rejects_unknown_value():
    """审批状态同样受 CHECK 限制, 不能靠写入非法取值来撤销一张已决定的审批"""
    session_id, _ = await create_session_with_message("run_appr_check")
    run_id = await create_run(session_id, thread_id="thread-check")
    async with SessionLocal() as db:
        execution = await tool_executions_crud.create_pending_execution(
            db, session_id, "run_shell", {"command": "ls"}, "call-check", run_id=run_id
        )
        approval = await approvals_crud.create_approval(db, session_id, run_id, "thread-check", execution.id)
        approval.status = "revoked"
        with pytest.raises(IntegrityError) as exc:
            await db.commit()
        await db.rollback()
    assert "ck_approvals_status" in str(exc.value)


async def test_second_active_run_in_same_session_is_rejected():
    """同一会话已有活动运行时, 数据库拒绝第二条, 不依赖应用层自觉"""
    session_id, _ = await create_session_with_message("run_admission_busy")
    await create_run(session_id, thread_id="thread-first")
    with pytest.raises(IntegrityError) as exc:
        await create_run(session_id, thread_id="thread-second")
    assert "uq_runs_one_active_session" in str(exc.value)


@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_terminal_run_frees_the_session_slot(status: RunStatus):
    """旧运行进入任一终态后释放名额, 同一会话可以登记新的运行"""
    session_id, _ = await create_session_with_message(f"slot_{status.value[:4]}")
    first = await create_run(session_id, thread_id="thread-first")
    async with SessionLocal() as db:
        run = await db.get(Run, first)
        run.status = status.value
        await db.commit()
    second = await create_run(session_id, thread_id="thread-second")
    assert (await load_run(second)).status == RunStatus.PENDING


async def test_request_id_index_only_constrains_non_null_keys():
    """幂等键唯一索引只约束非空键: 同键冲突, 空键可以并存"""
    first_session, _ = await create_session_with_message("run_req_a")
    second_session, _ = await create_session_with_message("run_req_b")
    await create_run(first_session, thread_id="thread-req-a", request_id="req-shared")
    with pytest.raises(IntegrityError) as exc:
        await create_run(second_session, thread_id="thread-req-b", request_id="req-shared")
    assert "uq_runs_request_id" in str(exc.value)

    third_session, _ = await create_session_with_message("run_req_c")
    await create_run(second_session, thread_id="thread-req-null-a")
    await create_run(third_session, thread_id="thread-req-null-b")  # 两条空键运行可以并存


async def test_thread_id_is_unique_across_runs():
    """一个 thread_id 只属于一条运行, 跨会话也不允许复用"""
    first_session, _ = await create_session_with_message("run_thread_a")
    second_session, _ = await create_session_with_message("run_thread_b")
    await create_run(first_session, thread_id="thread-shared")
    with pytest.raises(IntegrityError) as exc:
        await create_run(second_session, thread_id="thread-shared")
    assert "uq_runs_thread_id" in str(exc.value)


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
    run_id = await create_run(session_id, thread_id=thread_id, status=RunStatus.WAITING_APPROVAL.value)
    async with SessionLocal() as db:
        execution = await tool_executions_crud.create_pending_execution(
            db, session_id, "run_shell", {"command": "ls"}, "call-1", run_id=run_id
        )
        await approvals_crud.create_approval(db, session_id, run_id, thread_id, execution.id)
        await db.commit()
        rows = await db.execute(
            text("SELECT r.id, a.id FROM runs r JOIN approvals a ON a.thread_id = r.thread_id WHERE r.thread_id = :t"),
            {"t": thread_id},
        )
    assert len(rows.all()) == 1


async def test_approval_requires_an_owning_run():
    """审批必须挂在一条运行上: 缺 run_id 或指向不存在的运行都被数据库拒绝"""
    session_id, _ = await create_session_with_message("run_appr_owner")
    run_id = await create_run(session_id, thread_id="thread-owner")
    async with SessionLocal() as db:
        execution = await tool_executions_crud.create_pending_execution(
            db, session_id, "run_shell", {"command": "ls"}, "call-owner", run_id=run_id
        )
        await db.commit()
        execution_id = execution.id

    # 缺 run_id
    async with SessionLocal() as db:
        db.add(
            Approval(
                session_id=session_id,
                thread_id="thread-owner",
                tool_execution_id=execution_id,
                status="pending",
                scope="one_time",
            )
        )
        with pytest.raises(IntegrityError) as exc:
            await db.commit()
        await db.rollback()
    assert "run_id" in str(exc.value)

    # run_id 指向不存在的运行
    async with SessionLocal() as db:
        db.add(
            Approval(
                session_id=session_id,
                run_id=str(uuid4()),
                thread_id="thread-owner",
                tool_execution_id=execution_id,
                status="pending",
                scope="one_time",
            )
        )
        with pytest.raises(IntegrityError) as exc:
            await db.commit()
        await db.rollback()
    assert "approvals_run_id_fkey" in str(exc.value)

    # 归属明确时可以写入
    async with SessionLocal() as db:
        approval = await approvals_crud.create_approval(db, session_id, run_id, "thread-owner", execution_id)
        await db.commit()
    assert approval.run_id == run_id


async def test_approval_is_removed_with_its_run():
    """删除运行时名下审批级联删除, 归属关系不留悬空行"""
    session_id, _ = await create_session_with_message("run_appr_cascade")
    run_id = await create_run(session_id, thread_id="thread-cascade")
    async with SessionLocal() as db:
        execution = await tool_executions_crud.create_pending_execution(
            db, session_id, "run_shell", {"command": "ls"}, "call-cascade", run_id=run_id
        )
        await approvals_crud.create_approval(db, session_id, run_id, "thread-cascade", execution.id)
        await db.commit()
    async with SessionLocal() as db:
        await db.delete(await db.get(Run, run_id))
        await db.commit()
    async with SessionLocal() as db:
        rows = await db.execute(text("SELECT count(*) FROM approvals WHERE thread_id = 'thread-cascade'"))
    assert rows.scalar_one() == 0


# ---------- 迁移与模型的一致性 ----------


async def test_indexes_exist_in_database():
    """迁移建出的索引确实存在于库中, thread_id 的普通索引已被唯一索引取代"""
    async with SessionLocal() as db:
        rows = await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'runs'"))
    names = set(rows.scalars().all())
    assert {"ix_runs_session_id", "ix_runs_input_message_id", "uq_runs_thread_id"} <= names
    assert "ix_runs_thread_id" not in names


async def test_request_id_index_is_partial_and_unique():
    """幂等键索引在库里是带谓词的唯一索引, 不是普通索引"""
    async with SessionLocal() as db:
        rows = await db.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_runs_request_id'"))
        definition = rows.scalar_one()
    assert definition.startswith("CREATE UNIQUE INDEX")
    assert "WHERE (request_id IS NOT NULL)" in definition


async def test_admission_constraints_exist_in_database():
    """活动运行唯一索引的谓词与状态 CHECK 真实存在于库中, 不是只写在模型里"""
    async with SessionLocal() as db:
        index_rows = await db.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'runs'"))
        check_rows = await db.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'runs'::regclass AND contype = 'c'"
            )
        )
    indexes = dict(index_rows.all())
    checks = dict(check_rows.all())

    active_index = indexes["uq_runs_one_active_session"]
    assert active_index.startswith("CREATE UNIQUE INDEX")
    for status in (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL):
        assert status.value in active_index
    # 终态不占名额, 因此不在谓词里; 谓词一旦漏掉或多写, 这条就会红
    for status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
        assert status.value not in active_index

    for status in RunStatus:
        assert status.value in checks["ck_runs_status"]


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


async def test_approval_run_id_schema_matches_model():
    """run_id 的 NOT NULL、索引、级联外键与列注释真实存在于库中"""
    async with SessionLocal() as db:
        nullable = await db.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'approvals' AND column_name = 'run_id'"
            )
        )
        indexes = await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'approvals'"))
        fkeys = await db.execute(
            text(
                "SELECT conname, confdeltype::text FROM pg_constraint "
                "WHERE conrelid = 'approvals'::regclass AND contype = 'f'"
            )
        )
        comments = await db.execute(
            text(
                "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM pg_attribute a "
                "WHERE a.attrelid = 'approvals'::regclass AND a.attnum > 0 AND NOT a.attisdropped"
            )
        )
    assert nullable.scalar_one() == "NO"
    assert "ix_approvals_run_id" in set(indexes.scalars().all())
    assert dict(fkeys.all())["approvals_run_id_fkey"] == "c"  # c 表示级联删除
    expected = {column.name: column.comment for column in Approval.__table__.columns}
    assert dict(comments.all()) == expected
