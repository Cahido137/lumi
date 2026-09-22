"""集成测试: 运行记录 CRUD 与状态推进的数据库级行为。

Note:
    状态推进的合法性由 UPDATE 的 WHERE 条件在数据库侧原子判定, 因此这里断言的是
    「返回 True 且真的写入了」与「返回 False 且行未被修改」两种结果。
"""

import asyncio
from uuid import uuid4

import pytest
from app.crud import messages as messages_crud
from app.crud import runs as runs_crud
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.models import Run
from app.db.session import SessionLocal
from app.schemas.enums import MessageRole, RunStatus
from app.schemas.error_code import SessionErrorCode
from sqlalchemy import update

NON_TERMINAL = [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL]
"""非终态列表。"""

TERMINAL = [RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED]
"""终态列表。"""


async def create_session(username: str = "crud_tester") -> str:
    """创建一个用户与会话, 返回会话ID。"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "运行会话", user.id)
        await db.commit()
        return session.id


async def create_message(session_id: str, content: str = "你好") -> str:
    """在会话里插入一条用户消息, 返回消息ID。"""
    async with SessionLocal() as db:
        message = await messages_crud.add_message(db, session_id, MessageRole.USER, content)
        await db.commit()
        return message.id


async def new_run(session_id: str, thread_id: str = "thread-1", **overrides) -> str:
    """登记一条运行记录并返回主键。"""
    async with SessionLocal() as db:
        run = await runs_crud.create_run(db, session_id, thread_id, **overrides)
        await db.commit()
        return run.id


async def load(run_id: str) -> Run | None:
    """用全新会话读取运行记录。"""
    async with SessionLocal() as db:
        return await db.get(Run, run_id)


async def force_status(run_id: str, status: RunStatus) -> None:
    """直接把运行改成指定状态, 仅用于构造前置条件(绕过状态机)。"""
    async with SessionLocal() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(status=status.value))
        await db.commit()


# ---------- 创建与查询 ----------


async def test_create_run_registers_pending():
    """登记时状态为 pending, 计时列全部为空"""
    session_id = await create_session()
    run = await load(await new_run(session_id, thread_id="thread-a"))
    assert run.status == RunStatus.PENDING
    assert run.thread_id == "thread-a"
    assert run.attempt == 1
    assert run.input_message_id is None
    assert (run.started_at, run.finished_at, run.cancel_requested_at) == (None, None, None)


async def test_create_run_with_attempt_and_message():
    """输入消息与尝试次数按传入值落库"""
    session_id = await create_session("crud_attempt")
    message_id = await create_message(session_id)
    run = await load(await new_run(session_id, input_message_id=message_id, attempt=2))
    assert run.input_message_id == message_id
    assert run.attempt == 2


async def test_get_run_by_id_returns_none_for_unknown():
    """查询不存在的运行返回 None 而不是抛异常"""
    async with SessionLocal() as db:
        assert await runs_crud.get_run_by_id(db, str(uuid4())) is None


@pytest.mark.parametrize("status", NON_TERMINAL)
async def test_get_active_run_finds_non_terminal(status: RunStatus):
    """三种非终态都能被查到"""
    session_id = await create_session(f"a_{status.value}")
    run_id = await new_run(session_id)
    await force_status(run_id, status)
    async with SessionLocal() as db:
        active = await runs_crud.get_active_run(db, session_id)
    assert active is not None
    assert active.id == run_id


@pytest.mark.parametrize("status", TERMINAL)
async def test_get_active_run_ignores_terminal(status: RunStatus):
    """三种终态都不算「正在运行」"""
    session_id = await create_session(f"d_{status.value}")
    await force_status(await new_run(session_id), status)
    async with SessionLocal() as db:
        assert await runs_crud.get_active_run(db, session_id) is None


async def test_get_active_run_is_scoped_to_session_and_skips_history():
    """只查本会话, 且跳过本会话已经终结的历史运行"""
    session_id = await create_session("crud_scope")
    other_id = await create_session("crud_scope_other")
    history = await new_run(session_id, thread_id="thread-old")
    await force_status(history, RunStatus.SUCCEEDED)  # 历史轮次先终结, 名额才轮得到下一轮
    await asyncio.sleep(0.01)
    current = await new_run(session_id, thread_id="thread-new")
    await new_run(other_id, thread_id="thread-other")
    async with SessionLocal() as db:
        active = await runs_crud.get_active_run(db, session_id)
    assert active.id == current
    assert active.id != history


async def test_list_runs_for_session_orders_newest_first_and_pages():
    """历史列表按时间倒序, 分页参数生效"""
    session_id = await create_session("crud_list")
    ids = []
    for index in range(3):
        run_id = await new_run(session_id, thread_id=f"thread-{index}")
        ids.append(run_id)
        if index < 2:
            await force_status(run_id, RunStatus.SUCCEEDED)  # 每会话只允许一条活动运行, 历史轮次先终结
        await asyncio.sleep(0.01)
    async with SessionLocal() as db:
        first_page = await runs_crud.list_runs_for_session(db, session_id, limit=2)
        second_page = await runs_crud.list_runs_for_session(db, session_id, skip=2, limit=2)
    assert [item.id for item in first_page] == list(reversed(ids))[:2]
    assert [item.id for item in second_page] == [ids[0]]


# ---------- 状态推进 ----------


async def test_mark_run_started_sets_started_at_and_updated_at():
    """取得执行权时写入 started_at, created_at 保持不变"""
    session_id = await create_session("crud_started")
    run_id = await new_run(session_id)
    created_at = (await load(run_id)).created_at
    await asyncio.sleep(0.01)
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_started(db, run_id) is True
        await db.commit()
    run = await load(run_id)
    assert run.status == RunStatus.RUNNING
    assert run.started_at is not None
    assert run.started_at > created_at
    assert run.created_at == created_at
    assert run.updated_at > created_at
    assert run.finished_at is None


async def test_mark_run_started_twice_keeps_first_started_at():
    """running -> running 不在流转表里, 第二次推进失败且不覆盖原始时刻"""
    session_id = await create_session("crud_started_twice")
    run_id = await new_run(session_id)
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_started(db, run_id) is True
        await db.commit()
    first_started_at = (await load(run_id)).started_at
    await asyncio.sleep(0.01)
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_started(db, run_id) is False
        await db.commit()
    assert (await load(run_id)).started_at == first_started_at


async def test_mark_run_started_resumes_from_waiting_approval():
    """审批恢复路径: waiting_approval 可以重新回到 running"""
    session_id = await create_session("crud_resume")
    run_id = await new_run(session_id)
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_started(db, run_id) is True
        assert await runs_crud.mark_run_waiting_approval(db, run_id) is True
        await db.commit()
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_started(db, run_id) is True
        await db.commit()
    assert (await load(run_id)).status == RunStatus.RUNNING


async def test_mark_run_waiting_approval_only_from_running():
    """pending 不能直接跳到等待审批, 且等待审批不写 finished_at"""
    session_id = await create_session("crud_waiting")
    run_id = await new_run(session_id)
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_waiting_approval(db, run_id) is False
        assert await runs_crud.mark_run_started(db, run_id) is True
        assert await runs_crud.mark_run_waiting_approval(db, run_id) is True
        await db.commit()
    run = await load(run_id)
    assert run.status == RunStatus.WAITING_APPROVAL
    assert run.finished_at is None


async def test_mark_run_succeeded_sets_finished_at():
    """正常结束时写入 finished_at"""
    session_id = await create_session("crud_succeeded")
    run_id = await new_run(session_id)
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_started(db, run_id) is True
        assert await runs_crud.mark_run_succeeded(db, run_id) is True
        await db.commit()
    run = await load(run_id)
    assert run.status == RunStatus.SUCCEEDED
    assert run.finished_at is not None
    assert run.finished_at >= run.started_at
    assert run.error_code is None


async def test_mark_run_failed_records_stable_error_code():
    """失败时错误码落库, 枚举成员可直接作为字符串写入"""
    session_id = await create_session("crud_failed")
    run_id = await new_run(session_id)
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_started(db, run_id) is True
        assert await runs_crud.mark_run_failed(db, run_id, error_code=SessionErrorCode.RUN_IN_PROGRESS) is True
        await db.commit()
    run = await load(run_id)
    assert run.status == RunStatus.FAILED
    assert run.error_code == "run_in_progress"
    assert run.finished_at is not None


@pytest.mark.parametrize("status", NON_TERMINAL)
async def test_mark_run_cancelled_from_each_non_terminal(status: RunStatus):
    """三种非终态都可以被打断"""
    session_id = await create_session(f"c_{status.value}")
    run_id = await new_run(session_id)
    await force_status(run_id, status)
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_cancelled(db, run_id) is True
        await db.commit()
    run = await load(run_id)
    assert run.status == RunStatus.CANCELLED
    assert run.finished_at is not None


@pytest.mark.parametrize("status", TERMINAL)
async def test_terminal_run_rejects_every_advance(status: RunStatus):
    """终态是出口: 任何推进都失败且行保持原样"""
    session_id = await create_session(f"t_{status.value}")
    run_id = await new_run(session_id)
    await force_status(run_id, status)
    before = await load(run_id)
    snapshot = (before.status, before.started_at, before.finished_at, before.error_code)
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_started(db, run_id) is False
        assert await runs_crud.mark_run_waiting_approval(db, run_id) is False
        assert await runs_crud.mark_run_succeeded(db, run_id) is False
        assert await runs_crud.mark_run_failed(db, run_id, error_code="internal_error") is False
        assert await runs_crud.mark_run_cancelled(db, run_id) is False
        await db.commit()
    after = await load(run_id)
    assert (after.status, after.started_at, after.finished_at, after.error_code) == snapshot


async def test_advance_on_missing_run_returns_false():
    """运行不存在时全部写操作返回 False, 不抛异常"""
    missing = str(uuid4())
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_started(db, missing) is False
        assert await runs_crud.mark_run_waiting_approval(db, missing) is False
        assert await runs_crud.mark_run_succeeded(db, missing) is False
        assert await runs_crud.mark_run_failed(db, missing, error_code="internal_error") is False
        assert await runs_crud.mark_run_cancelled(db, missing) is False
        assert await runs_crud.request_run_cancel(db, missing) is False
        await db.commit()


# ---------- 打断请求与尝试序号 ----------


async def test_request_run_cancel_records_first_request_only():
    """只登记第一次打断请求的时刻, 且不改变运行状态"""
    session_id = await create_session("crud_cancel_request")
    run_id = await new_run(session_id)
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_started(db, run_id) is True
        assert await runs_crud.request_run_cancel(db, run_id) is True
        await db.commit()
    first_requested_at = (await load(run_id)).cancel_requested_at
    await asyncio.sleep(0.01)
    async with SessionLocal() as db:
        assert await runs_crud.request_run_cancel(db, run_id) is False
        await db.commit()
    run = await load(run_id)
    assert run.cancel_requested_at == first_requested_at
    assert run.status == RunStatus.RUNNING
    assert run.finished_at is None


async def test_next_attempt_counts_per_input_message():
    """尝试序号按「会话 + 输入消息」分别计数"""
    session_id = await create_session("crud_attempt_no")
    other_id = await create_session("crud_attempt_other")
    message_id = await create_message(session_id)
    another_message_id = await create_message(session_id, "第二条")
    async with SessionLocal() as db:
        assert await runs_crud.next_attempt(db, session_id, message_id) == 1
    await new_run(session_id, input_message_id=message_id)
    async with SessionLocal() as db:
        assert await runs_crud.next_attempt(db, session_id, message_id) == 2
        assert await runs_crud.next_attempt(db, session_id, another_message_id) == 1
        assert await runs_crud.next_attempt(db, other_id, message_id) == 1
