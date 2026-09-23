"""集成测试: 审批决定的原子性与幂等收敛(真库真图, 模型与工具用假的)。"""

import asyncio

import pytest
from app.core.graph import builder
from app.core.session_runner import resume_agent_session, run_agent_session
from app.crud import approvals as approvals_crud
from app.crud import runs as runs_crud
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.models import Approval, Message, Run
from app.db.session import SessionLocal
from app.schemas.enums import ApprovalScope, ApprovalStatus, MessageRole, RunStatus
from app.utils.errors import ConflictError
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from tests.fakes import FakePlanner, FakeTool, ScriptedModel

DECIDE_TIMEOUT = 10.0
"""并发用例的超时秒数。"""


def tool_call(name, args, call_id):
    """构造模型工具调用"""
    return {"name": name, "args": args, "id": call_id}


def patch_agent_deps(monkeypatch, model, tools=None):
    """替换全局图单例运行时的模型/计划器/工具"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: FakePlanner([]))
    monkeypatch.setattr(builder, "get_planner_structured_method", lambda: "function_calling")
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools or {})


async def create_user_and_session(username="appr_atomic"):
    """创建用户与会话, 返回会话ID"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "审批会话", user.id)
        await db.commit()
        return session.id


async def start_approval_run(monkeypatch, session_id, tool, final="执行完毕"):
    """跑一轮会触发审批中断的对话"""
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


async def get_runs(session_id) -> list[Run]:
    """取会话全部运行记录, 按登记时间正序"""
    async with SessionLocal() as db:
        result = await db.execute(select(Run).where(Run.session_id == session_id).order_by(Run.created_at, Run.id))
        return list(result.scalars())


async def get_approval(session_id) -> Approval:
    """取会话的审批单"""
    async with SessionLocal() as db:
        approval = await db.scalar(select(Approval).where(Approval.session_id == session_id))
    assert approval is not None
    return approval


async def get_approvals(session_id) -> list[Approval]:
    """取会话全部审批单, 按创建时间正序"""
    async with SessionLocal() as db:
        result = await db.execute(
            select(Approval).where(Approval.session_id == session_id).order_by(Approval.created_at, Approval.id)
        )
        return list(result.scalars())


async def count_messages(session_id, role, content=None) -> int:
    """统计会话里指定角色的消息条数, 给定 content 时只统计正文相同的"""
    async with SessionLocal() as db:
        conditions = [Message.session_id == session_id, Message.role == role]
        if content is not None:
            conditions.append(Message.content == content)
        stmt = select(func.count()).select_from(Message).where(*conditions)
        return int((await db.execute(stmt)).scalar_one())


async def test_concurrent_decide_only_one_wins(monkeypatch):
    """T05: 两个事务同时决定同一张审批单, 只有一个写入成功"""
    sid = await create_user_and_session("appr_race")
    await start_approval_run(monkeypatch, sid, FakeTool("run_shell", result="目录列表"))
    approval_id = (await get_approval(sid)).id
    barrier = asyncio.Barrier(2)

    async def decide(status: ApprovalStatus) -> bool:
        """在独立事务里决定一次, 屏障对齐两路的写入时刻"""
        async with SessionLocal() as db:
            await barrier.wait()
            decided = await approvals_crud.update_approval(db, approval_id, status, ApprovalScope.ONE_TIME)
            await db.commit()
            return decided

    results = await asyncio.wait_for(
        asyncio.gather(decide(ApprovalStatus.APPROVED), decide(ApprovalStatus.REJECTED)), timeout=DECIDE_TIMEOUT
    )
    assert results == [True, False] or results == [False, True]
    winner = ApprovalStatus.APPROVED if results[0] else ApprovalStatus.REJECTED
    after = await get_approval(sid)
    assert after.status == winner.value
    assert after.decided_at is not None


async def test_concurrent_resume_executes_once(monkeypatch):
    """T04: 两路同时批准同一张审批单, 只执行一次工具, 另一路重放第一次结果"""
    sid = await create_user_and_session("appr_once")
    tool = FakeTool("run_shell", result="目录列表")
    await start_approval_run(monkeypatch, sid, tool)
    approval_id = (await get_approval(sid)).id
    barrier = asyncio.Barrier(2)

    async def resume() -> str | None:
        """屏障对齐后各恢复一次"""
        await barrier.wait()
        return await resume_agent_session(approval_id, ApprovalStatus.APPROVED)

    replies = await asyncio.wait_for(asyncio.gather(resume(), resume()), timeout=DECIDE_TIMEOUT)
    assert replies == ["执行完毕", "执行完毕"]
    assert tool.calls == [{"command": "dir"}]
    runs = await get_runs(sid)
    assert len(runs) == 1
    assert runs[0].status == RunStatus.SUCCEEDED
    assert await count_messages(sid, MessageRole.ASSISTANT.value, "执行完毕") == 1
    assert await count_messages(sid, MessageRole.TOOL.value) == 1


async def test_same_decision_replay_returns_first_reply(monkeypatch):
    """同决定同范围重发返回第一次的回复, 不再执行工具也不新增消息"""
    sid = await create_user_and_session("appr_replay")
    tool = FakeTool("run_shell", result="目录列表")
    await start_approval_run(monkeypatch, sid, tool)
    approval_id = (await get_approval(sid)).id
    assert await resume_agent_session(approval_id, ApprovalStatus.APPROVED) == "执行完毕"
    before = await count_messages(sid, MessageRole.ASSISTANT.value)

    # 图已经跑完, 换成空脚本模型作为陷阱: 重放若进图会抛 IndexError
    patch_agent_deps(monkeypatch, ScriptedModel([]))
    assert await resume_agent_session(approval_id, ApprovalStatus.APPROVED) == "执行完毕"
    assert tool.calls == [{"command": "dir"}]
    assert len(await get_runs(sid)) == 1
    assert await count_messages(sid, MessageRole.ASSISTANT.value) == before


async def test_conflicting_decision_is_rejected(monkeypatch):
    """已批准的审批单再拒绝返回冲突, 且不改动已终态的运行"""
    sid = await create_user_and_session("appr_conflict")
    await start_approval_run(monkeypatch, sid, FakeTool("run_shell", result="目录列表"))
    approval_id = (await get_approval(sid)).id
    assert await resume_agent_session(approval_id, ApprovalStatus.APPROVED) == "执行完毕"

    patch_agent_deps(monkeypatch, ScriptedModel([]))
    with pytest.raises(ConflictError, match="审批决定与已有决定冲突"):
        await resume_agent_session(approval_id, ApprovalStatus.REJECTED)
    runs = await get_runs(sid)
    assert len(runs) == 1
    assert runs[0].status == RunStatus.SUCCEEDED
    assert (await get_approval(sid)).status == ApprovalStatus.APPROVED.value


async def test_conflicting_scope_is_rejected(monkeypatch):
    """同决定但授权范围不同同样返回冲突, 已写入的范围不被改写"""
    sid = await create_user_and_session("appr_scope")
    await start_approval_run(monkeypatch, sid, FakeTool("run_shell", result="目录列表"))
    approval_id = (await get_approval(sid)).id
    assert await resume_agent_session(approval_id, ApprovalStatus.APPROVED, ApprovalScope.ONE_TIME) == "执行完毕"

    patch_agent_deps(monkeypatch, ScriptedModel([]))
    with pytest.raises(ConflictError, match="审批决定与已有决定冲突"):
        await resume_agent_session(approval_id, ApprovalStatus.APPROVED, ApprovalScope.TOOL)
    assert (await get_approval(sid)).scope == ApprovalScope.ONE_TIME.value


async def test_claim_failure_does_not_execute_or_decide(monkeypatch):
    """领取失败没有执行: 运行无法流转时不进图, 审批决定整体回滚"""
    sid = await create_user_and_session("appr_claim")
    await start_approval_run(monkeypatch, sid, FakeTool("run_shell", result="目录列表"))
    approval = await get_approval(sid)
    run_id = (await get_runs(sid))[0].id
    # 造一条已经终态的运行, 模拟执行权无从领取
    async with SessionLocal() as db:
        assert await runs_crud.mark_run_failed(db, run_id, error_code="internal_error")
        await db.commit()

    patch_agent_deps(monkeypatch, ScriptedModel([]))
    with pytest.raises(ConflictError, match="运行状态不合法"):
        await resume_agent_session(approval.id, ApprovalStatus.APPROVED)
    after = await get_approval(sid)
    assert after.status == ApprovalStatus.PENDING.value
    assert after.decided_at is None
    assert (await get_runs(sid))[0].status == RunStatus.FAILED


async def test_approval_records_owning_run(monkeypatch):
    """审批中断产生的审批单带上所属运行ID, 与 thread_id 指向同一条运行"""
    sid = await create_user_and_session("appr_owner")
    await start_approval_run(monkeypatch, sid, FakeTool("run_shell", result="目录列表"))
    run = (await get_runs(sid))[0]
    approval = await get_approval(sid)
    assert approval.run_id == run.id
    assert approval.thread_id == run.thread_id


async def test_second_approval_after_resume_shares_same_run(monkeypatch):
    """恢复后再次中断产生的新审批仍归属同一条运行, 不新建运行"""
    sid = await create_user_and_session("appr_twice")
    tool = FakeTool("run_shell", result="目录列表")
    patch_agent_deps(
        monkeypatch,
        ScriptedModel(
            [
                AIMessage(content="", tool_calls=[tool_call("run_shell", {"command": "dir"}, "c1")]),
                AIMessage(content="", tool_calls=[tool_call("run_shell", {"command": "pwd"}, "c2")]),
                AIMessage(content="两次都执行完了"),
            ]
        ),
        tools={"run_shell": tool},
    )
    assert await run_agent_session(sid, "列两次目录") is None
    run_id = (await get_runs(sid))[0].id
    first = (await get_approvals(sid))[0]
    assert first.run_id == run_id

    # 批准第一次后再次中断, 返回 None 表示又停在等待审批
    assert await resume_agent_session(first.id, ApprovalStatus.APPROVED) is None
    approvals = await get_approvals(sid)
    assert len(approvals) == 2
    assert len(await get_runs(sid)) == 1
    assert approvals[1].id != first.id
    assert approvals[1].run_id == run_id
    assert approvals[1].thread_id == first.thread_id
    assert tool.calls == [{"command": "dir"}]
