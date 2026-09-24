"""集成测试: 运行命令表的数据库级保证与领取协议(真库, 模型与工具用假的)。"""

import asyncio
from uuid import uuid4

import pytest
from app.core.graph import builder
from app.core.session_runner import runner
from app.core.session_runner.runner import run_agent_session
from app.core.session_runner.state import RunCancelledError
from app.core.session_runner.submission import submit_run
from app.crud import run_commands as run_commands_crud
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.models import Run, RunCommand
from app.db.session import SessionLocal
from app.schemas.enums import RunCommandKind, RunCommandStatus, RunStatus
from app.utils.errors import ConflictError
from langchain_core.messages import AIMessage
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from tests.fakes import FakePlanner, ScriptedModel

CLAIM_TIMEOUT = 10.0
"""并发用例的超时秒数。"""


def patch_agent_deps(monkeypatch, model, tools=None):
    """替换全局图单例运行时的模型/计划器/工具"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: FakePlanner([]))
    monkeypatch.setattr(builder, "get_planner_structured_method", lambda: "function_calling")
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools or {})


async def create_user_and_session(username="cmd_test"):
    """创建用户与会话, 返回会话ID"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "命令会话", user.id)
        await db.commit()
        return session.id


async def create_run(session_id, thread_id, status=RunStatus.PENDING.value) -> str:
    """直接插入一条运行记录, 返回运行ID"""
    async with SessionLocal() as db:
        run = Run(session_id=session_id, thread_id=thread_id, status=status)
        db.add(run)
        await db.commit()
        return run.id


async def make_command(run_id, kind=RunCommandKind.START, approval_id=None) -> str:
    """登记一条命令, 返回命令ID"""
    async with SessionLocal() as db:
        command = await run_commands_crud.create_command(db, run_id, kind, approval_id=approval_id)
        command_id = command.id
        await db.commit()
        return command_id


async def load_run(run_id) -> Run:
    """取一条运行记录"""
    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
    assert run is not None
    return run


async def load_command(command_id) -> RunCommand:
    """取一条命令"""
    async with SessionLocal() as db:
        command = await db.get(RunCommand, command_id)
    assert command is not None
    return command


async def list_command_statuses(run_id) -> list[str]:
    """取一条运行名下全部命令的状态, 按登记时间正序"""
    async with SessionLocal() as db:
        return [command.status for command in await run_commands_crud.list_commands(db, run_id)]


# ---------- schema 与约束 ----------


async def test_run_commands_schema_matches_model():
    """命令表的 CHECK、偏唯一索引、级联外键与列注释真实存在于库中"""
    async with SessionLocal() as db:
        indexes = await db.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'run_commands'"))
        checks = await db.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'run_commands'::regclass AND contype = 'c'"
            )
        )
        fkeys = await db.execute(
            text(
                "SELECT conname, confdeltype::text FROM pg_constraint "
                "WHERE conrelid = 'run_commands'::regclass AND contype = 'f'"
            )
        )
        comments = await db.execute(
            text(
                "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM pg_attribute a "
                "WHERE a.attrelid = 'run_commands'::regclass AND a.attnum > 0 AND NOT a.attisdropped"
            )
        )
    index_defs = dict(indexes.all())
    check_defs = dict(checks.all())

    # 每条运行同时最多一个未完成命令, 终态不占名额
    active = index_defs["uq_run_commands_one_active"]
    assert active.startswith("CREATE UNIQUE INDEX")
    assert RunCommandStatus.PENDING.value in active
    assert RunCommandStatus.CLAIMED.value in active
    for status in (RunCommandStatus.COMPLETED, RunCommandStatus.CANCELLED, RunCommandStatus.FAILED):
        assert status.value not in active

    for kind in RunCommandKind:
        assert kind.value in check_defs["ck_run_commands_kind"]
    for status in RunCommandStatus:
        assert status.value in check_defs["ck_run_commands_status"]
    # c 表示级联删除
    assert dict(fkeys.all()) == {"run_commands_approval_id_fkey": "c", "run_commands_run_id_fkey": "c"}
    expected = {column.name: column.comment for column in RunCommand.__table__.columns}
    assert dict(comments.all()) == expected


async def test_one_active_command_per_run():
    """同一条运行已有未完成命令时, 数据库拒绝第二条"""
    sid = await create_user_and_session("cmd_unique")
    run_id = await create_run(sid, f"{sid}:cmd")
    await make_command(run_id)
    with pytest.raises(IntegrityError) as exc:
        await make_command(run_id)
    assert "uq_run_commands_one_active" in str(exc.value)


async def test_completed_command_allows_next_command():
    """命令完成后同一条运行可以登记下一条, 恢复命令不与旧执行命令重叠"""
    sid = await create_user_and_session("cmd_next")
    run_id = await create_run(sid, f"{sid}:cmd")
    first = await make_command(run_id)
    async with SessionLocal() as db:
        assert await run_commands_crud.claim_command(db, first)
        assert await run_commands_crud.advance_command(db, first, RunCommandStatus.COMPLETED)
        await db.commit()
    second = await make_command(run_id, RunCommandKind.RESUME)
    assert second != first
    assert await list_command_statuses(run_id) == [
        RunCommandStatus.COMPLETED.value,
        RunCommandStatus.PENDING.value,
    ]


async def test_command_check_constraints_reject_unknown_values():
    """kind 与 status 都受 CHECK 限制, 不能靠写入非法取值绕过流转表"""
    sid = await create_user_and_session("cmd_check")
    run_id = await create_run(sid, f"{sid}:cmd")
    for column, value in (("kind", "restart"), ("status", "done")):
        fields = {"run_id": run_id, "kind": "start", "status": "pending"}
        fields[column] = value  # 只把待测列换成非法取值
        async with SessionLocal() as db:
            db.add(RunCommand(**fields))
            with pytest.raises(IntegrityError) as exc:
                await db.commit()
            await db.rollback()
        assert f"ck_run_commands_{column}" in str(exc.value)


async def test_command_removed_with_its_run():
    """删除运行时名下命令级联删除, 归属关系不留悬空行"""
    sid = await create_user_and_session("cmd_cascade")
    run_id = await create_run(sid, f"{sid}:cascade")
    await make_command(run_id)
    async with SessionLocal() as db:
        await db.delete(await db.get(Run, run_id))
        await db.commit()
    async with SessionLocal() as db:
        rows = await db.execute(text("SELECT count(*) FROM run_commands WHERE run_id = :r"), {"r": run_id})
    assert rows.scalar_one() == 0


# ---------- 领取与流转 ----------


async def test_concurrent_claim_only_one_wins():
    """两个事务同时领取同一条命令, 只有一个成功, 任期与投递计数只加一次"""
    sid = await create_user_and_session("cmd_claim")
    run_id = await create_run(sid, f"{sid}:cmd")
    command_id = await make_command(run_id)
    barrier = asyncio.Barrier(2)

    async def claim() -> bool:
        """在独立事务里领取一次, 屏障对齐两路的写入时刻"""
        async with SessionLocal() as db:
            await barrier.wait()
            claimed = await run_commands_crud.claim_command(db, command_id)
            await db.commit()
            return claimed

    results = await asyncio.wait_for(asyncio.gather(claim(), claim()), timeout=CLAIM_TIMEOUT)
    assert results == [True, False] or results == [False, True]
    command = await load_command(command_id)
    assert command.status == RunCommandStatus.CLAIMED.value
    assert command.claimed_epoch == 1
    assert command.delivery_attempt == 1


async def test_command_transition_table_is_enforced():
    """命令只能按流转表推进, 进入终态之后不再接受任何写入"""
    sid = await create_user_and_session("cmd_transition")
    run_id = await create_run(sid, f"{sid}:cmd")
    command_id = await make_command(run_id)
    async with SessionLocal() as db:
        # 未领取不能直接完成
        assert not await run_commands_crud.advance_command(db, command_id, RunCommandStatus.COMPLETED)
        assert await run_commands_crud.claim_command(db, command_id)
        assert await run_commands_crud.advance_command(db, command_id, RunCommandStatus.COMPLETED)
        for target in RunCommandStatus:
            assert not await run_commands_crud.advance_command(db, command_id, target)
        await db.commit()
    assert (await load_command(command_id)).status == RunCommandStatus.COMPLETED.value


async def test_cancel_pending_commands_skips_claimed():
    """取消只处理待领取的命令, 已领取的交给核对流程, 不复制出并行命令"""
    sid = await create_user_and_session("cmd_cancel")
    run_id = await create_run(sid, f"{sid}:cancel")
    done_id = await make_command(run_id)
    async with SessionLocal() as db:
        assert await run_commands_crud.claim_command(db, done_id)
        assert await run_commands_crud.advance_command(db, done_id, RunCommandStatus.COMPLETED)
        await db.commit()
    claimed_id = await make_command(run_id, RunCommandKind.RESUME)
    async with SessionLocal() as db:
        assert await run_commands_crud.claim_command(db, claimed_id)
        await db.commit()
    other_run = await create_run(sid, f"{sid}:other", status=RunStatus.SUCCEEDED.value)
    other_id = await make_command(other_run)

    async with SessionLocal() as db:
        assert await run_commands_crud.cancel_pending_commands(db, run_id) == 0
        assert await run_commands_crud.cancel_pending_commands(db, other_run) == 1
        await db.commit()
    assert (await load_command(done_id)).status == RunCommandStatus.COMPLETED.value
    assert (await load_command(claimed_id)).status == RunCommandStatus.CLAIMED.value
    assert (await load_command(other_id)).status == RunCommandStatus.CANCELLED.value


async def test_advance_command_on_missing_row_returns_false():
    """命令不存在时条件更新返回 False, 不抛异常"""
    async with SessionLocal() as db:
        assert not await run_commands_crud.claim_command(db, str(uuid4()))
        assert not await run_commands_crud.advance_command(db, str(uuid4()), RunCommandStatus.COMPLETED)
        await db.commit()


# ---------- 与运行器接线 ----------


async def test_submit_registers_start_command():
    """受理提交时初始命令与输入、运行记录同事务落库, 运行留在排队等领取"""
    sid = await create_user_and_session("cmd_submit")
    submission = await submit_run(sid, "你好", request_id="req-start")
    run = await load_run(submission.run_id)
    assert run.status == RunStatus.PENDING.value
    assert run.started_at is None
    command = await load_command(submission.command_id)
    assert command.run_id == submission.run_id
    assert command.kind == RunCommandKind.START.value
    assert command.status == RunCommandStatus.PENDING.value
    assert command.approval_id is None


async def test_start_claim_failure_does_not_execute(monkeypatch):
    """领取返回零行时不跑图、不发布开始事件, 运行与命令都不被改写"""
    patch_agent_deps(monkeypatch, ScriptedModel([]))  # 陷阱: 进图就会抛 IndexError
    published = []

    async def collect(event):
        published.append(event.event_type)

    monkeypatch.setattr(runner.event_bus, "publish", collect)
    sid = await create_user_and_session("cmd_norun")
    submission = await submit_run(sid, "你好", request_id="req-norun")
    # 运行被别处推进到终态, 领取条件不再满足
    async with SessionLocal() as db:
        await db.execute(text("UPDATE runs SET status = 'succeeded' WHERE id = :r"), {"r": submission.run_id})
        await db.commit()

    with pytest.raises(ConflictError):
        await runner._execute_submission(sid, "你好", submission)

    assert published == []
    assert (await load_run(submission.run_id)).status == RunStatus.SUCCEEDED.value
    assert (await load_command(submission.command_id)).status == RunCommandStatus.PENDING.value


async def test_cancelled_run_leaves_no_pending_command(monkeypatch):
    """受理之后被取消: 运行收尾为打断, 名下待领取的初始命令一并取消"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="不会用到")]))
    sid = await create_user_and_session("cmd_orphan")
    generations = iter([1, 2])
    monkeypatch.setattr(runner, "get_cancel_generation", lambda session_id: next(generations))
    with pytest.raises(RunCancelledError):
        await run_agent_session(sid, "你好")

    async with SessionLocal() as db:
        run = (await db.execute(select(Run).where(Run.session_id == sid))).scalars().one()
        active = await run_commands_crud.get_active_command(db, run.id)
    assert run.status == RunStatus.CANCELLED.value
    assert active is None  # 终态运行名下不留未完成命令
    assert await list_command_statuses(run.id) == [RunCommandStatus.CANCELLED.value]


async def test_successful_run_completes_its_command(monkeypatch):
    """一轮正常对话结束后初始命令记为 completed, 运行记为 succeeded"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="你好, 有什么可以帮你")]))
    sid = await create_user_and_session("cmd_ok")
    reply = await run_agent_session(sid, "你好")
    assert reply.content == "你好, 有什么可以帮你"
    async with SessionLocal() as db:
        run = (await db.execute(select(Run).where(Run.session_id == sid))).scalars().one()
    assert run.status == RunStatus.SUCCEEDED.value
    assert await list_command_statuses(run.id) == [RunCommandStatus.COMPLETED.value]
