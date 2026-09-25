"""集成测试: 运行提交协议(幂等键、待决审批准入、输入与运行同事务)。"""

import asyncio
from uuid import uuid4

import pytest
from app.core.graph import builder
from app.core.session_runner import resume_agent_session, runner
from app.core.session_runner.runner import run_agent_session
from app.core.session_runner.state import RunCancelledError
from app.core.session_runner.submission import Submission, submit_run
from app.crud import approvals as approvals_crud
from app.crud import run_commands as run_commands_crud
from app.crud import runs as runs_crud
from app.crud import sessions as sessions_crud
from app.crud import tool_executions as tool_executions_crud
from app.crud import users as users_crud
from app.db.models import Approval, Message, Run
from app.db.session import SessionLocal
from app.schemas.enums import ApprovalStatus, MessageRole, RunCommandKind, RunCommandStatus, RunStatus
from app.schemas.error_code import CommonErrorCode, SessionErrorCode
from app.utils.errors import ConflictError
from langchain_core.messages import AIMessage
from sqlalchemy import func, select, update
from tests.fakes import FakePlanner, FakeTool, ScriptedModel


def tool_call(name, args, call_id):
    """构造模型工具调用"""
    return {"name": name, "args": args, "id": call_id}


def patch_agent_deps(monkeypatch, model, tools=None):
    """替换全局图单例运行时的模型/计划器/工具"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: FakePlanner([]))
    monkeypatch.setattr(builder, "get_planner_structured_method", lambda: "function_calling")
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools or {})


async def create_user_and_session(username="submit_test"):
    """创建用户与会话, 返回会话ID"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "提交会话", user.id)
        await db.commit()
        return session.id


async def get_runs(session_id) -> list[Run]:
    """取会话全部运行记录, 按登记时间正序"""
    async with SessionLocal() as db:
        result = await db.execute(select(Run).where(Run.session_id == session_id).order_by(Run.created_at, Run.id))
        return list(result.scalars())


async def count_messages(session_id, role: MessageRole) -> int:
    """统计会话里指定角色的消息条数"""
    async with SessionLocal() as db:
        stmt = (
            select(func.count())
            .select_from(Message)
            .where(Message.session_id == session_id, Message.role == role.value)
        )
        return int((await db.execute(stmt)).scalar_one())


async def get_message_content(message_id) -> str:
    """取一条消息的正文"""
    async with SessionLocal() as db:
        return (await db.get(Message, message_id)).content


async def force_status(run_id, status: RunStatus) -> None:
    """直接把运行改成指定状态, 仅用于构造前置条件(绕过状态机)"""
    async with SessionLocal() as db:
        await db.execute(update(Run).where(Run.id == run_id).values(status=status.value))
        await db.commit()


async def make_pending_approval(session_id, thread_id="thread-approval") -> str:
    """造一条等待审批的运行与它名下的待决审批, 返回审批单ID"""
    async with SessionLocal() as db:
        run = await runs_crud.create_run(db, session_id, thread_id)
        run_id = run.id
        execution = await tool_executions_crud.create_pending_execution(
            db, session_id, "run_shell", {"command": "ls"}, "call-1", run_id=run_id
        )
        approval = await approvals_crud.create_approval(db, session_id, run_id, thread_id, execution.id)
        await db.commit()
    await force_status(run_id, RunStatus.WAITING_APPROVAL)
    return approval.id


async def get_pending_approval_id(session_id) -> str:
    """取会话的审批单ID"""
    async with SessionLocal() as db:
        result = await db.execute(select(Approval.id).where(Approval.session_id == session_id))
        return result.scalar_one()


# ---------- 受理与同事务 ----------


async def test_submit_records_input_and_run_together():
    """受理一次提交: 输入、运行记录与初始命令一起落库, 运行留在排队等领取"""
    sid = await create_user_and_session("submit_basic")
    submission = await submit_run(sid, "第一个问题", request_id="req-basic")

    assert submission.replayed is False
    async with SessionLocal() as db:
        run = await runs_crud.get_run_by_id(db, submission.run_id)
        message = await db.get(Message, submission.input_message_id)
        command = await run_commands_crud.get_command_by_id(db, submission.command_id)
    # 执行权由消费者领取, 受理本身不推进运行
    assert run.status == RunStatus.PENDING.value
    assert run.started_at is None
    assert run.input_message_id == message.id
    assert run.request_id == "req-basic"
    assert len(run.request_fingerprint) == 64
    assert message.role == MessageRole.USER.value
    assert message.content == "第一个问题"
    assert command is not None
    assert command.run_id == run.id
    assert command.kind == RunCommandKind.START.value
    assert command.status == RunCommandStatus.PENDING.value


async def test_submit_without_request_id_skips_idempotency():
    """不传幂等键时两次提交是两条运行, 幂等键与摘要都留空"""
    sid = await create_user_and_session("submit_nokey")
    first = await submit_run(sid, "同样的问题")
    await force_status(first.run_id, RunStatus.SUCCEEDED)
    second = await submit_run(sid, "同样的问题")

    assert second.run_id != first.run_id
    async with SessionLocal() as db:
        run = await runs_crud.get_run_by_id(db, second.run_id)
    assert run.request_id is None
    assert run.request_fingerprint is None


# ---------- T01 / T02 ----------


async def test_concurrent_submit_with_same_request_id_creates_one_run():
    """T01: 同 request_id 并发提交两次, 只有一条运行与一条输入, 两次拿到同一个 run_id"""
    sid = await create_user_and_session("submit_t01")
    barrier = asyncio.Barrier(2)

    async def submit():
        await barrier.wait()  # 两个提交同时冲向数据库, 不靠 sleep 编排顺序
        return await submit_run(sid, "同一个问题", request_id="req-t01")

    first, second = await asyncio.gather(submit(), submit())

    assert first.run_id == second.run_id
    assert sorted([first.replayed, second.replayed]) == [False, True]
    assert len(await get_runs(sid)) == 1
    assert await count_messages(sid, MessageRole.USER) == 1


async def test_concurrent_submit_to_same_session_accepts_only_one():
    """T03: 同会话两个不同请求同时提交, 只受理一个, 另一个拿到活动运行冲突"""
    sid = await create_user_and_session("submit_t03")
    barrier = asyncio.Barrier(2)

    async def submit(content, key):
        await barrier.wait()  # 两个提交同时冲向数据库, 不靠 sleep 编排顺序
        return await submit_run(sid, content, request_id=key)

    results = await asyncio.gather(submit("问题一", "req-t03-a"), submit("问题二", "req-t03-b"), return_exceptions=True)
    accepted = [item for item in results if isinstance(item, Submission)]
    rejected = [item for item in results if isinstance(item, ConflictError)]

    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0].error_code == SessionErrorCode.RUN_IN_PROGRESS
    assert len(await get_runs(sid)) == 1
    assert await count_messages(sid, MessageRole.USER) == 1


async def test_same_request_id_with_different_content_is_rejected():
    """T02: 同 request_id 提交不同内容返回冲突, 原运行的输入不被改写"""
    sid = await create_user_and_session("submit_t02")
    first = await submit_run(sid, "原始内容", request_id="req-t02")

    with pytest.raises(ConflictError) as exc:
        await submit_run(sid, "被改写的内容", request_id="req-t02")

    assert exc.value.error_code == CommonErrorCode.CONFLICT
    assert exc.value.detail["request_id"] == "req-t02"
    assert len(await get_runs(sid)) == 1
    assert await get_message_content(first.input_message_id) == "原始内容"


async def test_same_content_with_different_request_id_is_a_new_run():
    """内容重复但幂等键不同就是两次请求, 旧运行终结后可以再受理一条"""
    sid = await create_user_and_session("submit_t02b")
    first = await submit_run(sid, "同样的内容", request_id="req-a")
    await force_status(first.run_id, RunStatus.SUCCEEDED)
    second = await submit_run(sid, "同样的内容", request_id="req-b")

    assert second.run_id != first.run_id
    assert second.replayed is False
    assert await count_messages(sid, MessageRole.USER) == 2


# ---------- 准入 ----------


async def test_pending_approval_blocks_submit_with_stable_error_code():
    """存在待决审批时提交被拒, 用稳定错误码, 不新增运行记录与输入"""
    sid = await create_user_and_session("submit_blocked")
    await make_pending_approval(sid)

    with pytest.raises(ConflictError) as exc:
        await submit_run(sid, "新对话", request_id="req-blocked")

    assert exc.value.error_code == SessionErrorCode.PENDING_APPROVAL_EXISTS
    runs = await get_runs(sid)
    assert len(runs) == 1  # 只有前置条件里那条等待审批的运行
    assert runs[0].status == RunStatus.WAITING_APPROVAL
    assert await count_messages(sid, MessageRole.USER) == 0


async def test_active_run_conflict_maps_to_run_in_progress_and_leaves_no_input():
    """已有活动运行时数据库拦下提交, 映射成稳定错误码, 输入消息随事务一起回滚"""
    sid = await create_user_and_session("submit_busy")
    async with SessionLocal() as db:
        busy = await runs_crud.create_run(db, sid, f"{sid}:busy")
        await runs_crud.mark_run_started(db, busy.id)
        await db.commit()

    with pytest.raises(ConflictError) as exc:
        await submit_run(sid, "挤不进去的问题", request_id="req-busy")

    assert exc.value.error_code == SessionErrorCode.RUN_IN_PROGRESS
    assert await count_messages(sid, MessageRole.USER) == 0
    assert len(await get_runs(sid)) == 1


async def test_replay_wins_over_busy_session():
    """会话再忙, 同一个已受理的请求仍然返回原来那条运行, 而不是报冲突"""
    sid = await create_user_and_session("submit_replay_busy")
    first = await submit_run(sid, "问题", request_id="req-replay-busy")

    again = await submit_run(sid, "问题", request_id="req-replay-busy")

    assert again.run_id == first.run_id
    assert again.replayed is True
    assert len(await get_runs(sid)) == 1


# ---------- 重放 ----------


async def test_replay_returns_original_reply_without_rerunning_graph(monkeypatch):
    """同键重放返回首次那条助手消息, 图不再被调用"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="第一次回答")]))
    sid = await create_user_and_session("submit_replay")
    first = await run_agent_session(sid, "问题", request_id="req-replay")

    patch_agent_deps(monkeypatch, ScriptedModel([]))  # 空脚本当陷阱: 图一旦被调用就会抛 IndexError
    second = await run_agent_session(sid, "问题", request_id="req-replay")

    assert second.id == first.id
    assert second.content == "第一次回答"
    assert len(await get_runs(sid)) == 1
    assert await count_messages(sid, MessageRole.USER) == 1
    assert await count_messages(sid, MessageRole.ASSISTANT) == 1
    assert (await get_runs(sid))[0].output_message_id == first.id


async def test_replay_of_waiting_approval_returns_none(monkeypatch):
    """等待审批的运行被同键重放时返回 None, 由路由给出等待审批文案"""
    tool = FakeTool("run_shell", result="目录列表")
    patch_agent_deps(
        monkeypatch,
        ScriptedModel(
            [
                AIMessage(content="", tool_calls=[tool_call("run_shell", {"command": "dir"}, "c1")]),
                AIMessage(content="执行完毕"),
            ]
        ),
        tools={"run_shell": tool},
    )
    sid = await create_user_and_session("submit_replay_wait")
    assert await run_agent_session(sid, "列目录", request_id="req-wait") is None

    patch_agent_deps(monkeypatch, ScriptedModel([]))
    assert await run_agent_session(sid, "列目录", request_id="req-wait") is None
    assert len(await get_runs(sid)) == 1


async def test_replay_after_approval_resume_returns_final_reply(monkeypatch):
    """审批恢复成功后也记下产出消息, 首次提交的重放才能拿到回复"""
    tool = FakeTool("run_shell", result="目录列表")
    patch_agent_deps(
        monkeypatch,
        ScriptedModel(
            [
                AIMessage(content="", tool_calls=[tool_call("run_shell", {"command": "dir"}, "c1")]),
                AIMessage(content="执行完毕"),
            ]
        ),
        tools={"run_shell": tool},
    )
    sid = await create_user_and_session("submit_replay_resume")
    assert await run_agent_session(sid, "列目录", request_id="req-resume") is None
    assert await resume_agent_session(await get_pending_approval_id(sid), ApprovalStatus.APPROVED) == "执行完毕"

    patch_agent_deps(monkeypatch, ScriptedModel([]))
    replayed = await run_agent_session(sid, "列目录", request_id="req-resume")

    assert replayed.content == "执行完毕"
    assert len(await get_runs(sid)) == 1


async def test_replay_of_cancelled_run_raises_cancelled():
    """被打断的运行被重放时抛打断异常, 与首次调用的响应保持一致"""
    sid = await create_user_and_session("submit_replay_cancel")
    submission = await submit_run(sid, "问题", request_id="req-cancel")
    await force_status(submission.run_id, RunStatus.CANCELLED)

    with pytest.raises(RunCancelledError):
        await runner._replay_submission(submission.run_id)


async def test_replay_of_failed_run_is_a_conflict():
    """已结束却没有产出回复的运行不能被重放, 明确报冲突而不是假成功"""
    sid = await create_user_and_session("submit_replay_fail")
    submission = await submit_run(sid, "问题", request_id="req-fail")
    await force_status(submission.run_id, RunStatus.FAILED)

    with pytest.raises(ConflictError):
        await runner._replay_submission(submission.run_id)


async def test_replay_of_still_running_run_reports_in_progress():
    """重放撞上仍在进行的运行时报活动运行冲突, 不重复跑图"""
    sid = await create_user_and_session("submit_replay_run")
    submission = await submit_run(sid, "问题", request_id="req-run")

    with pytest.raises(ConflictError) as exc:
        await runner._replay_submission(submission.run_id)

    assert exc.value.error_code == SessionErrorCode.RUN_IN_PROGRESS


async def test_replay_of_missing_run_is_a_conflict():
    """运行记录不存在时明确报冲突, 不静默返回空回复"""
    with pytest.raises(ConflictError):
        await runner._replay_submission(str(uuid4()))
