"""集成测试: 运行记录在会话运行器里的生命周期(真库真图, 模型与工具用假的)。"""

import asyncio

import pytest
from app.core.graph import builder
from app.core.session_runner import resume_agent_session, retry_agent_session, run_agent_session, runner
from app.core.session_runner.state import RunCancelledError, request_cancel_session
from app.crud import runs as runs_crud
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.models import Approval, Message, Run
from app.db.session import SessionLocal
from app.schemas.enums import ApprovalStatus, MessageRole, RunStatus
from app.schemas.error_code import SessionErrorCode
from app.utils.errors import ConflictError
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from tests.fakes import FakePlanner, FakeTool, ScriptedModel, SlowModel


class RaisingModel:
    """每次调用都抛出指定异常的假模型。"""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def ainvoke(self, messages, **kwargs):
        raise self.error


def tool_call(name, args, call_id):
    """构造模型工具调用"""
    return {"name": name, "args": args, "id": call_id}


def patch_agent_deps(monkeypatch, model, tools=None):
    """替换全局图单例运行时的模型/计划器/工具"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: FakePlanner([]))
    monkeypatch.setattr(builder, "get_planner_structured_method", lambda: "function_calling")
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools or {})


def patch_history_failure(monkeypatch, error: Exception) -> None:
    """让运行在登记之后、跑图之前失败, 用于确定性地触发失败出口。"""

    async def boom(db, session_id, exclude_id=None):
        raise error

    monkeypatch.setattr(runner, "rebuild_history", boom)


async def create_user_and_session(username="run_life"):
    """创建用户与会话, 返回会话ID"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "运行会话", user.id)
        await db.commit()
        return session.id


async def get_runs(session_id) -> list[Run]:
    """取会话全部运行记录, 按登记时间正序"""
    async with SessionLocal() as db:
        result = await db.execute(select(Run).where(Run.session_id == session_id).order_by(Run.created_at, Run.id))
        return list(result.scalars())


async def get_approval(session_id) -> Approval | None:
    """取会话的审批单"""
    async with SessionLocal() as db:
        return await db.scalar(select(Approval).where(Approval.session_id == session_id))


async def count_user_messages(session_id) -> int:
    """统计会话里的用户消息条数"""
    async with SessionLocal() as db:
        stmt = (
            select(func.count())
            .select_from(Message)
            .where(Message.session_id == session_id, Message.role == MessageRole.USER.value)
        )
        return int((await db.execute(stmt)).scalar_one())


async def first_user_message_id(session_id) -> str:
    """取会话第一条用户消息的ID"""
    async with SessionLocal() as db:
        result = await db.execute(
            select(Message.id)
            .where(Message.session_id == session_id, Message.role == MessageRole.USER.value)
            .order_by(Message.created_at, Message.id)
            .limit(1)
        )
        return result.scalar_one()


async def start_approval_run(monkeypatch, session_id, final="执行完毕") -> None:
    """跑一轮会触发审批中断的对话"""
    tool = FakeTool("run_shell", result="目录列表")
    patch_agent_deps(
        monkeypatch,
        ScriptedModel(
            [
                AIMessage(content="", tool_calls=[tool_call("run_shell", {"command": "dir"}, "c1")]),
                AIMessage(content=final),
            ]
        ),
        tools={"run_shell": tool},
    )
    assert await run_agent_session(session_id, "列目录") is None


# ---------- 正常与审批路径 ----------


async def test_successful_run_is_recorded(monkeypatch):
    """成功轮次留下一条 succeeded 运行, 输入消息与计时列都齐全"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="你好, 有什么可以帮你")]))
    sid = await create_user_and_session("life_ok")
    reply = await run_agent_session(sid, "你好")
    assert reply.content == "你好, 有什么可以帮你"
    runs = await get_runs(sid)
    assert len(runs) == 1
    run = runs[0]
    assert run.status == RunStatus.SUCCEEDED
    assert run.attempt == 1
    assert run.input_message_id == await first_user_message_id(sid)
    assert run.thread_id.startswith(f"{sid}:")
    assert run.error_code is None
    assert run.cancel_requested_at is None
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.finished_at >= run.started_at


async def test_approval_interrupt_leaves_run_waiting_approval(monkeypatch):
    """审批中断时运行停在 waiting_approval, 且 thread_id 与审批单一致"""
    sid = await create_user_and_session("life_wait")
    await start_approval_run(monkeypatch, sid)
    run = (await get_runs(sid))[0]
    approval = await get_approval(sid)
    assert run.status == RunStatus.WAITING_APPROVAL
    assert run.finished_at is None
    assert run.error_code is None
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING.value
    assert run.thread_id == approval.thread_id


async def test_resume_after_approval_completes_same_run(monkeypatch):
    """批准恢复后是同一条运行走到 succeeded, 不新建也不改 attempt"""
    sid = await create_user_and_session("life_resume_ok")
    await start_approval_run(monkeypatch, sid)
    run_id = (await get_runs(sid))[0].id
    approval_id = (await get_approval(sid)).id
    reply = await resume_agent_session(approval_id, ApprovalStatus.APPROVED)
    assert reply == "执行完毕"
    runs = await get_runs(sid)
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].status == RunStatus.SUCCEEDED
    assert runs[0].attempt == 1
    assert runs[0].finished_at is not None


async def test_resume_after_rejection_completes_same_run(monkeypatch):
    """拒绝审批同样让这条运行正常收尾为 succeeded"""
    sid = await create_user_and_session("life_resume_no")
    await start_approval_run(monkeypatch, sid, final="好的, 已取消")
    run_id = (await get_runs(sid))[0].id
    approval_id = (await get_approval(sid)).id
    reply = await resume_agent_session(approval_id, ApprovalStatus.REJECTED)
    assert reply == "好的, 已取消"
    runs = await get_runs(sid)
    assert len(runs) == 1
    assert runs[0].id == run_id
    assert runs[0].status == RunStatus.SUCCEEDED


async def test_resume_failure_marks_run_failed_and_keeps_approval(monkeypatch):
    """恢复失败时运行判为 failed, 已作出的批准保留, 同一张审批单不能再次决定"""
    sid = await create_user_and_session("life_resume_fail")
    await start_approval_run(monkeypatch, sid)
    run_id = (await get_runs(sid))[0].id
    approval_id = (await get_approval(sid)).id

    # 第一次恢复: 模型抛异常
    patch_agent_deps(monkeypatch, RaisingModel(RuntimeError("模型炸了")))
    with pytest.raises(RuntimeError):
        await resume_agent_session(approval_id, ApprovalStatus.APPROVED)

    run = (await get_runs(sid))[0]
    assert run.id == run_id
    assert run.status == RunStatus.FAILED  # 失败事实明确, 不再退回等待审批
    assert run.error_code == "internal_error"
    assert run.finished_at is not None
    # 批准是不可回退事实: 决定已提交, 不会退回 pending
    assert (await get_approval(sid)).status == ApprovalStatus.APPROVED.value

    # 同决定重发不再进入执行, 已失败的运行没有可重放的回复
    with pytest.raises(ConflictError, match="没有可重放的回复"):
        await resume_agent_session(approval_id, ApprovalStatus.APPROVED)
    assert (await get_runs(sid))[0].status == RunStatus.FAILED


async def test_finalize_does_not_override_terminal_run(monkeypatch):
    """取消先提交时, 后到的成功收尾不得改写终态, 且冲突必须留下可检测记录"""
    sid = await create_user_and_session("life_race")
    async with SessionLocal() as db:
        run = await runs_crud.create_run(db, sid, f"{sid}:race")
        await runs_crud.mark_run_started(db, run.id)
        await runs_crud.mark_run_cancelled(db, run.id)
        await db.commit()
        run_id = run.id

    warnings: list[str] = []
    monkeypatch.setattr(runner.logger, "warning", lambda msg, *args: warnings.append(msg % args))
    await runner._finalize_run(run_id, RunStatus.SUCCEEDED)

    async with SessionLocal() as db:
        final = await db.get(Run, run_id)
    assert final.status == RunStatus.CANCELLED, "已提交的终态被后到的收尾改写了"
    assert final.finished_at is not None
    assert any("运行收尾未生效" in item for item in warnings), "收尾未生效却没有留下可检测记录"


# ---------- 失败与打断路径 ----------


async def test_failure_after_registration_marks_run_failed(monkeypatch):
    """登记之后失败: 运行标记 failed 并写入 internal_error"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="不会用到")]))
    sid = await create_user_and_session("life_fail")
    patch_history_failure(monkeypatch, RuntimeError("重建历史失败"))
    with pytest.raises(RuntimeError):
        await run_agent_session(sid, "你好")
    run = (await get_runs(sid))[0]
    assert run.status == RunStatus.FAILED
    assert run.error_code == "internal_error"
    assert run.finished_at is not None


async def test_business_exception_error_code_is_recorded(monkeypatch):
    """业务异常的错误码原样记录, 不退化成 internal_error"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="不会用到")]))
    sid = await create_user_and_session("life_biz_err")
    patch_history_failure(monkeypatch, ConflictError("会话状态冲突"))
    with pytest.raises(ConflictError):
        await run_agent_session(sid, "你好")
    assert (await get_runs(sid))[0].error_code == "conflict"


async def test_cancelled_run_has_no_error_code(monkeypatch):
    """打断收尾为 cancelled, 不写错误码(打断不是错误)"""
    patch_agent_deps(monkeypatch, SlowModel(seconds=30))
    sid = await create_user_and_session("life_cancel")
    task = asyncio.create_task(run_agent_session(sid, "开始长任务"))
    await asyncio.sleep(0.3)
    assert request_cancel_session(sid) is True
    with pytest.raises(RunCancelledError):
        await task
    run = (await get_runs(sid))[0]
    assert run.status == RunStatus.CANCELLED
    assert run.error_code is None
    assert run.finished_at is not None


async def test_finalize_failure_does_not_mask_original_error(monkeypatch):
    """收尾自身失败时原始异常仍然原样传播, 运行卡在 running 等租约兜底"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="不会用到")]))
    sid = await create_user_and_session("life_mask")
    patch_history_failure(monkeypatch, RuntimeError("原始错误"))

    async def boom(db, run_id, *, error_code):
        raise RuntimeError("收尾失败")

    monkeypatch.setattr(runs_crud, "mark_run_failed", boom)
    with pytest.raises(RuntimeError) as exc:
        await run_agent_session(sid, "你好")
    assert "原始错误" in str(exc.value)
    assert (await get_runs(sid))[0].status == RunStatus.RUNNING


# ---------- 不跑图的路径 ----------


async def test_pending_approval_blocks_new_run_without_creating_row(monkeypatch):
    """存在待审批时新一轮被拒绝, 既不新增运行记录也不动旧记录"""
    sid = await create_user_and_session("life_blocked")
    await start_approval_run(monkeypatch, sid)
    assert len(await get_runs(sid)) == 1
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="不会用到")]))
    with pytest.raises(ConflictError) as exc:
        await run_agent_session(sid, "新对话")
    assert exc.value.error_code == SessionErrorCode.PENDING_APPROVAL_EXISTS
    runs = await get_runs(sid)
    assert len(runs) == 1
    assert runs[0].status == RunStatus.WAITING_APPROVAL


async def test_generation_mismatch_cancels_accepted_run(monkeypatch):
    """受理之后被取消: 这一轮不跑图, 已受理的运行收尾为打断, 输入保留"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="不会用到")]))
    sid = await create_user_and_session("life_gen")
    generations = iter([1, 2])
    monkeypatch.setattr(runner, "get_cancel_generation", lambda session_id: next(generations))
    with pytest.raises(RunCancelledError):
        await run_agent_session(sid, "你好")
    runs = await get_runs(sid)
    assert len(runs) == 1
    assert runs[0].status == RunStatus.CANCELLED
    assert runs[0].finished_at is not None
    assert runs[0].error_code is None
    assert await count_user_messages(sid) == 1


# ---------- 重试 ----------


async def test_retry_creates_new_run_with_next_attempt(monkeypatch):
    """重试新建一条 attempt=2 的运行, 旧运行原样保留(审计账本)"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="旧回答"), AIMessage(content="新回答")]))
    sid = await create_user_and_session("life_retry")
    first = await run_agent_session(sid, "旧问题")
    assert first.content == "旧回答"
    message_id = await first_user_message_id(sid)
    second = await retry_agent_session(sid, message_id, "新问题")
    assert second.content == "新回答"
    runs = await get_runs(sid)
    assert len(runs) == 2
    assert [(item.attempt, item.status) for item in runs] == [
        (1, RunStatus.SUCCEEDED),
        (2, RunStatus.SUCCEEDED),
    ]
    assert runs[0].input_message_id == message_id
    assert runs[1].input_message_id == message_id


async def test_retry_while_waiting_approval_terminates_old_run(monkeypatch):
    """等待审批时重试: 旧运行被终结且记录保留, 新运行接管同一条输入"""
    patch_agent_deps(
        monkeypatch,
        ScriptedModel(
            [
                AIMessage(content="", tool_calls=[tool_call("run_shell", {"command": "dir"}, "c1")]),
                AIMessage(content="重试后的回答"),
            ]
        ),
        tools={"run_shell": FakeTool("run_shell", result="目录列表")},
    )
    sid = await create_user_and_session("life_retry_wait")
    assert await run_agent_session(sid, "列目录") is None
    old_run = (await get_runs(sid))[0]
    assert old_run.status == RunStatus.WAITING_APPROVAL
    message_id = await first_user_message_id(sid)

    reply = await retry_agent_session(sid, message_id, None)
    assert reply.content == "重试后的回答"

    runs = await get_runs(sid)
    assert len(runs) == 2
    by_attempt = {item.attempt: item for item in runs}
    # 旧运行进了终态, 但记录本身保留, 不是被删掉
    assert by_attempt[1].id == old_run.id
    assert by_attempt[1].status == RunStatus.CANCELLED
    assert by_attempt[1].finished_at is not None
    # 新运行接管, 重试链靠 input_message_id + attempt 保持可追溯
    assert by_attempt[2].status == RunStatus.SUCCEEDED
    assert by_attempt[1].input_message_id == message_id
    assert by_attempt[2].input_message_id == message_id
    # 待决审批已随既有清理逻辑删除, 不会阻塞新一轮
    assert await get_approval(sid) is None


async def test_retry_while_running_returns_conflict(monkeypatch):
    """仍有运行在执行时重试返回冲突, 且不删除任何既有记录"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="旧回答")]))
    sid = await create_user_and_session("life_retry_busy")
    first = await run_agent_session(sid, "旧问题")
    assert first.content == "旧回答"
    message_id = await first_user_message_id(sid)
    # 造一条仍在执行中的运行, 模拟"还有执行方没停下"
    async with SessionLocal() as db:
        busy = await runs_crud.create_run(db, sid, f"{sid}:busy")
        await runs_crud.mark_run_started(db, busy.id)
        await db.commit()
        busy_id = busy.id
    async with SessionLocal() as db:
        before = len(list((await db.execute(select(Message).where(Message.session_id == sid))).scalars()))

    with pytest.raises(ConflictError):
        await retry_agent_session(sid, message_id, "新问题")

    async with SessionLocal() as db:
        after = len(list((await db.execute(select(Message).where(Message.session_id == sid))).scalars()))
        busy_run = await db.get(Run, busy_id)
    assert after == before, "被拒绝的重试仍然删除了消息"
    assert busy_run.status == RunStatus.RUNNING, "被拒绝的重试改写了运行状态"
    assert [(item.attempt, item.status) for item in await get_runs(sid)] == [
        (1, RunStatus.SUCCEEDED),
        (1, RunStatus.RUNNING),
    ]
