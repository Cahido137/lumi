"""集成测试: 审批决定的原子性与幂等收敛(真库真图, 模型与工具用假的)。"""

import asyncio

import pytest
from app.core.graph import builder
from app.core.session_runner import resume_agent_session, run_agent_session, runner
from app.core.session_runner.consumer import claim_execution
from app.crud import approvals as approvals_crud
from app.crud import run_commands as run_commands_crud
from app.crud import runs as runs_crud
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.models import Approval, Message, Run, RunCommand, ToolExecution
from app.db.session import SessionLocal
from app.schemas.enums import (
    ApprovalScope,
    ApprovalStatus,
    EventType,
    ExecutionStatus,
    MessageRole,
    RunCommandKind,
    RunCommandStatus,
    RunStatus,
)
from app.utils.errors import ConflictError
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from tests.fakes import FakePlanner, FakeTool, ScriptedModel


class RaisingAfterScriptModel:
    """先按脚本返回, 脚本用尽后抛出指定异常的假模型。"""

    def __init__(self, responses, error: Exception) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if not self.responses:
            raise self.error
        return self.responses.pop(0)


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


async def get_run(run_id) -> Run:
    """取一条运行记录"""
    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
    assert run is not None
    return run


async def get_command(command_id) -> RunCommand:
    """取一条运行命令"""
    async with SessionLocal() as db:
        command = await db.get(RunCommand, command_id)
    assert command is not None
    return command


async def get_commands(run_id) -> list[RunCommand]:
    """取一条运行名下的全部命令, 按登记时间正序"""
    async with SessionLocal() as db:
        return await run_commands_crud.list_commands(db, run_id)


async def get_execution(session_id) -> ToolExecution:
    """取会话的工具执行记录"""
    async with SessionLocal() as db:
        execution = await db.scalar(select(ToolExecution).where(ToolExecution.session_id == session_id))
    assert execution is not None
    return execution


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


async def test_concurrent_resume_creates_single_command(monkeypatch):
    """T04: 两路同时批准只产生一条恢复命令, 两条命令都随运行结果收尾"""
    sid = await create_user_and_session("appr_cmd")
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
    run = (await get_runs(sid))[0]
    commands = await get_commands(run.id)
    # 一条初始命令 + 一条恢复命令, 落败的那一路没有多出命令
    assert [command.kind for command in commands] == [RunCommandKind.START.value, RunCommandKind.RESUME.value]
    assert {command.status for command in commands} == {RunCommandStatus.COMPLETED.value}
    assert commands[1].approval_id == approval_id


async def test_resume_command_survives_api_crash_and_stays_claimable(monkeypatch):
    """T10: 决定事务提交后进程死亡, 恢复命令仍在库里且能被另一个执行方领取"""
    sid = await create_user_and_session("appr_crash")
    await start_approval_run(monkeypatch, sid, FakeTool("run_shell", result="目录列表"))
    approval = await get_approval(sid)
    run = (await get_runs(sid))[0]

    # 只跑决定事务, 等价于 API 在提交之后、领取执行之前立刻崩溃
    queued = await runner._decide_and_queue(
        approval.id, approval.thread_id, ApprovalStatus.APPROVED, ApprovalScope.ONE_TIME
    )
    assert queued is not None
    run_id, command_id = queued
    assert run_id == run.id

    # 崩溃之后留在库里的事实: 决定已提交、运行排队、命令待领取
    assert (await get_approval(sid)).status == ApprovalStatus.APPROVED.value
    assert (await get_run(run_id)).status == RunStatus.PENDING.value
    command = await get_command(command_id)
    assert command.kind == RunCommandKind.RESUME.value
    assert command.status == RunCommandStatus.PENDING.value
    assert command.approval_id == approval.id

    # 另一个执行方(等价于独立 Worker)仍能领取这条命令
    assert await claim_execution(run_id, command_id) is True
    assert (await get_run(run_id)).status == RunStatus.RUNNING.value
    claimed = await get_command(command_id)
    assert claimed.status == RunCommandStatus.CLAIMED.value
    assert claimed.claimed_epoch == 1
    assert claimed.delivery_attempt == 1

    # 重复领取失败, 不会复制出并行命令
    assert await claim_execution(run_id, command_id) is False
    assert (await get_command(command_id)).claimed_epoch == 1


async def test_resume_publishes_each_event_once(monkeypatch):
    """一次成功恢复里审批结束与运行结束事件各只发一次, 不重复推送"""
    sid = await create_user_and_session("appr_events")
    await start_approval_run(monkeypatch, sid, FakeTool("run_shell", result="目录列表"))
    approval_id = (await get_approval(sid)).id

    published = []

    async def collect(event):
        """只记录事件类型, 不投递给订阅者"""
        published.append(event.event_type)

    monkeypatch.setattr(runner.event_bus, "publish", collect)
    assert await resume_agent_session(approval_id, ApprovalStatus.APPROVED) == "执行完毕"
    assert published.count(EventType.APPROVAL_RESULT) == 1
    assert published.count(EventType.AGENT_FINISHED) == 1


async def test_model_failure_after_tool_success_keeps_both_facts(monkeypatch):
    """T15: 工具成功后模型失败, 批准事实与工具成功事实都保留"""
    sid = await create_user_and_session("appr_t15")
    tool = FakeTool("run_shell", result="目录列表")
    # 第一阶段: 模型只发出工具调用, 图停在审批中断, 工具还没执行
    patch_agent_deps(
        monkeypatch,
        ScriptedModel([AIMessage(content="", tool_calls=[tool_call("run_shell", {"command": "dir"}, "c1")])]),
        tools={"run_shell": tool},
    )
    assert await run_agent_session(sid, "列目录") is None
    approval = await get_approval(sid)
    run_id = (await get_runs(sid))[0].id
    assert tool.calls == []
    assert (await get_execution(sid)).status == ExecutionStatus.PENDING.value

    # 第二阶段: 批准后工具真的执行成功, 随后模型炸掉
    failing = RaisingAfterScriptModel([], RuntimeError("模型在工具成功后炸了"))
    patch_agent_deps(monkeypatch, failing, tools={"run_shell": tool})
    with pytest.raises(RuntimeError, match="模型在工具成功后炸了"):
        await resume_agent_session(approval.id, ApprovalStatus.APPROVED)

    # 副作用已经发生: 工具确实被调用过, 模型确实被调用过一次
    assert tool.calls == [{"command": "dir"}]
    assert failing.calls == 1

    # 运行失败事实明确
    run = await get_run(run_id)
    assert run.status == RunStatus.FAILED.value
    assert run.error_code == "internal_error"
    assert run.finished_at is not None

    # 批准事实保留: 决定已提交, 不因执行失败回退
    after = await get_approval(sid)
    assert after.status == ApprovalStatus.APPROVED.value
    assert after.decided_at is not None

    # 工具成功事实保留: 状态与输出都不被后续模型失败改写
    execution = await get_execution(sid)
    assert execution.status == ExecutionStatus.SUCCESS.value
    assert execution.tool_output == "目录列表"
    assert execution.finished_at is not None

    # 初始命令保持 completed, 恢复命令记为 failed, 名下不留未完成命令
    commands = await get_commands(run_id)
    assert [c.kind for c in commands] == [RunCommandKind.START.value, RunCommandKind.RESUME.value]
    assert [c.status for c in commands] == [RunCommandStatus.COMPLETED.value, RunCommandStatus.FAILED.value]
