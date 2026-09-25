"""集成测试: 工具执行记录的运行归属与调用身份唯一性(真库真图, 模型与工具用假的)。"""

import pytest
from app.core.graph import builder
from app.core.session_runner import resume_agent_session, run_agent_session
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.models import Approval, Run, ToolExecution
from app.db.session import SessionLocal
from app.schemas.enums import ApprovalStatus, ExecutionStatus, RunStatus
from langchain_core.messages import AIMessage
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
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


async def create_user_and_session(username="exec_attr"):
    """创建用户与会话, 返回会话ID"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "归属会话", user.id)
        await db.commit()
        return session.id


async def create_run(session_id, thread_id, status=RunStatus.PENDING.value) -> str:
    """插入一条运行记录, 返回运行ID"""
    async with SessionLocal() as db:
        run = Run(session_id=session_id, thread_id=thread_id, status=status)
        db.add(run)
        await db.commit()
        return run.id


async def get_runs(session_id) -> list[Run]:
    """取会话全部运行记录"""
    async with SessionLocal() as db:
        result = await db.execute(select(Run).where(Run.session_id == session_id).order_by(Run.created_at, Run.id))
        return list(result.scalars())


async def get_approval(session_id) -> Approval:
    """取会话的审批单"""
    async with SessionLocal() as db:
        approval = await db.scalar(select(Approval).where(Approval.session_id == session_id))
    assert approval is not None
    return approval


async def list_executions(session_id) -> list[ToolExecution]:
    """取会话全部工具执行记录, 按开始时间正序"""
    async with SessionLocal() as db:
        result = await db.execute(
            select(ToolExecution)
            .where(ToolExecution.session_id == session_id)
            .order_by(ToolExecution.started_at, ToolExecution.id)
        )
        return list(result.scalars())


# ---------- schema 与约束 ----------


async def test_tool_execution_schema_matches_model():
    """run_id 的可空外键、偏唯一索引与全表列注释真实存在于库中"""
    async with SessionLocal() as db:
        nullable = await db.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'tool_executions' AND column_name = 'run_id'"
            )
        )
        indexes = await db.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'tool_executions'")
        )
        fkeys = await db.execute(
            text(
                "SELECT conname, confdeltype::text FROM pg_constraint "
                "WHERE conrelid = 'tool_executions'::regclass AND contype = 'f'"
            )
        )
        comments = await db.execute(
            text(
                "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM pg_attribute a "
                "WHERE a.attrelid = 'tool_executions'::regclass AND a.attnum > 0 AND NOT a.attisdropped"
            )
        )
    # 可空是刻意的: 无法归属的历史记录留在显式兼容路径, 不伪造归属
    assert nullable.scalar_one() == "YES"
    unique = dict(indexes.all())["uq_tool_execution_call"]
    assert unique.startswith("CREATE UNIQUE INDEX")
    assert "run_id IS NOT NULL" in unique
    assert "tool_call_id IS NOT NULL" in unique
    # c 表示级联删除
    assert dict(fkeys.all())["tool_executions_run_id_fkey"] == "c"
    expected = {column.name: column.comment for column in ToolExecution.__table__.columns}
    assert dict(comments.all()) == expected


async def test_one_execution_per_call_within_run():
    """同一运行内同一逻辑调用只允许一条主记录, 第二条被数据库拒绝"""
    sid = await create_user_and_session("exec_unique")
    run_id = await create_run(sid, f"{sid}:exec")

    async def insert() -> None:
        async with SessionLocal() as db:
            db.add(
                ToolExecution(
                    session_id=sid, run_id=run_id, tool_name="run_shell", tool_call_id="call-dup", tool_input={}
                )
            )
            await db.commit()

    await insert()
    with pytest.raises(IntegrityError) as exc:
        await insert()
    assert "uq_tool_execution_call" in str(exc.value)
    assert len(await list_executions(sid)) == 1


async def test_same_call_id_in_different_runs_is_allowed():
    """唯一性限定在运行内: 不同运行复用同一 tool_call_id 不冲突"""
    sid = await create_user_and_session("exec_scope")
    # 第一条先置终态, 否则会撞 uq_runs_one_active_session
    first = await create_run(sid, f"{sid}:one", RunStatus.SUCCEEDED.value)
    second = await create_run(sid, f"{sid}:two")
    for run_id in (first, second):
        async with SessionLocal() as db:
            db.add(
                ToolExecution(
                    session_id=sid, run_id=run_id, tool_name="read_file", tool_call_id="call-same", tool_input={}
                )
            )
            await db.commit()
    executions = await list_executions(sid)
    assert len(executions) == 2
    assert {e.run_id for e in executions} == {first, second}


async def test_legacy_execution_without_run_is_allowed():
    """run_id 为空的历史记录仍可存在, 同 tool_call_id 的多条互不冲突"""
    sid = await create_user_and_session("exec_legacy")
    async with SessionLocal() as db:
        for path in ("a", "b"):
            db.add(
                ToolExecution(
                    session_id=sid, tool_name="read_file", tool_call_id="call-legacy", tool_input={"path": path}
                )
            )
        await db.commit()
    executions = await list_executions(sid)
    assert len(executions) == 2
    assert all(execution.run_id is None for execution in executions)


async def test_execution_removed_with_its_run():
    """删除运行时名下工具记录级联删除, 归属关系不留悬空行"""
    sid = await create_user_and_session("exec_cascade")
    run_id = await create_run(sid, f"{sid}:cascade")
    async with SessionLocal() as db:
        db.add(
            ToolExecution(
                session_id=sid, run_id=run_id, tool_name="read_file", tool_call_id="call-cascade", tool_input={}
            )
        )
        await db.commit()
    async with SessionLocal() as db:
        await db.delete(await db.get(Run, run_id))
        await db.commit()
    async with SessionLocal() as db:
        rows = await db.execute(text("SELECT count(*) FROM tool_executions WHERE run_id = :r"), {"r": run_id})
    assert rows.scalar_one() == 0


# ---------- 与运行器接线 ----------


async def test_real_run_attributes_every_execution(monkeypatch):
    """真图跑一轮: 免审批与待审批两条写入路径都带上所属运行ID"""
    sid = await create_user_and_session("exec_e2e")
    reader = FakeTool("read_file", result="文件内容")
    shell = FakeTool("run_shell", result="目录列表")
    patch_agent_deps(
        monkeypatch,
        ScriptedModel(
            [
                AIMessage(content="", tool_calls=[tool_call("read_file", {"path": "a.txt"}, "c-free")]),
                AIMessage(content="", tool_calls=[tool_call("run_shell", {"command": "dir"}, "c-approval")]),
                AIMessage(content="都做完了"),
            ]
        ),
        tools={"read_file": reader, "run_shell": shell},
    )
    assert await run_agent_session(sid, "先读文件再列目录") is None
    run_id = (await get_runs(sid))[0].id

    by_call = {execution.tool_call_id: execution for execution in await list_executions(sid)}
    # 免审批工具走 create_finished_execution, 已经落库为成功
    assert by_call["c-free"].run_id == run_id
    assert by_call["c-free"].status == ExecutionStatus.SUCCESS.value
    assert by_call["c-free"].needs_approval is False
    # 待审批工具走 create_pending_execution, 停在 pending 等决定
    assert by_call["c-approval"].run_id == run_id
    assert by_call["c-approval"].status == ExecutionStatus.PENDING.value
    assert by_call["c-approval"].needs_approval is True
    assert shell.calls == []

    approval = await get_approval(sid)
    assert approval.run_id == run_id
    assert await resume_agent_session(approval.id, ApprovalStatus.APPROVED) == "都做完了"

    by_call = {execution.tool_call_id: execution for execution in await list_executions(sid)}
    assert by_call["c-approval"].status == ExecutionStatus.SUCCESS.value
    assert by_call["c-approval"].run_id == run_id
    assert by_call["c-approval"].finished_at is not None
    # 全程只有一条运行, 两个逻辑调用各一条主记录, 恢复没有复制出并行记录
    assert len(await get_runs(sid)) == 1
    assert len(by_call) == 2
    assert reader.calls == [{"path": "a.txt"}]
    assert shell.calls == [{"command": "dir"}]
    assert (await get_runs(sid))[0].status == RunStatus.SUCCEEDED.value
