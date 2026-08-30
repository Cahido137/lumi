"""集成测试: 真实测试库跑会话运行器, 模型/工具用假的"""
import asyncio

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core.graph import builder
from app.core.session_runner import (
    resume_agent_session,
    retry_agent_session,
    run_agent_session,
)
from app.core.session_runner.context import StreamResult
from app.core.session_runner.state import CANCEL_MESSAGE, RunCancelledError, request_cancel_session
from app.crud import sessions as sessions_crud
from app.crud import todos as todos_crud
from app.crud import users as users_crud
from app.db.models import Approval, Message, ToolExecution
from app.db.session import SessionLocal
from app.schemas.enums import ApprovalStatus, ExecutionStatus, MessageRole
from app.schemas.todos import TodoItem, TodoStatus
from tests.fakes import FakePlanner, FakeTool, ScriptedModel, SlowModel


def tool_call(name, args, call_id):
    """构造模型工具调用"""
    return {"name": name, "args": args, "id": call_id}


def patch_agent_deps(monkeypatch, model, tools=None):
    """替换全局图单例运行时的模型/计划器/工具"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: FakePlanner([]))
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools or {})


async def create_user_and_session(username="tester"):
    """创建用户与会话, 返回会话ID"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "新会话", user.id)
        await db.commit()
        return session.id


async def list_messages(session_id):
    """按时间正序取会话消息"""
    async with SessionLocal() as db:
        result = await db.execute(
            select(Message).where(Message.session_id == session_id)
            .order_by(Message.created_at, Message.id)
        )
        return list(result.scalars())


async def get_executions(session_id):
    """取会话全部工具执行记录"""
    async with SessionLocal() as db:
        result = await db.execute(
            select(ToolExecution).where(ToolExecution.session_id == session_id)
        )
        return list(result.scalars())


async def get_pending_approval_id(session_id):
    """取会话的待审批单ID"""
    async with SessionLocal() as db:
        return await db.scalar(
            select(Approval.id).where(Approval.session_id == session_id)
        )


async def test_normal_chat_persists_messages(monkeypatch):
    """场景1: 模型直接回答, 用户与助手消息落库, 无工具记录"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="你好, 有什么可以帮你")]))
    sid = await create_user_and_session()
    reply = await run_agent_session(sid, "你好")
    assert reply.content == "你好, 有什么可以帮你"
    msgs = await list_messages(sid)
    assert [m.role for m in msgs] == [MessageRole.USER.value, MessageRole.ASSISTANT.value]
    assert await get_executions(sid) == []


async def test_non_approval_tool_recorded(monkeypatch):
    """场景2: 非审批工具执行后落库, 入参与状态齐全"""
    tool = FakeTool("web_search", result="3条结果")
    patch_agent_deps(monkeypatch, ScriptedModel([
        AIMessage(content="", tool_calls=[tool_call("web_search", {"query": "新闻"}, "c1")]),
        AIMessage(content="搜索完成"),
    ]), tools={"web_search": tool})
    sid = await create_user_and_session()
    reply = await run_agent_session(sid, "搜新闻")
    assert reply.content == "搜索完成"
    rows = await get_executions(sid)
    assert len(rows) == 1
    assert rows[0].tool_name == "web_search"
    assert rows[0].needs_approval is False
    assert rows[0].status == ExecutionStatus.SUCCESS.value
    assert rows[0].tool_input == {"query": "新闻"}


async def test_approval_flow_approve(monkeypatch):
    """场景3: 审批中断->批准->恢复执行, 记录状态完整流转"""
    tool = FakeTool("run_shell", result="目录列表")
    patch_agent_deps(monkeypatch, ScriptedModel([
        AIMessage(content="", tool_calls=[tool_call("run_shell", {"command": "dir"}, "c1")]),
        AIMessage(content="执行完毕"),
    ]), tools={"run_shell": tool})
    sid = await create_user_and_session()
    assert await run_agent_session(sid, "列目录") is None  # 中断等待审批
    rows = await get_executions(sid)
    assert rows[0].status == ExecutionStatus.PENDING.value
    reply = await resume_agent_session(await get_pending_approval_id(sid), ApprovalStatus.APPROVED)
    assert reply == "执行完毕"
    assert (await get_executions(sid))[0].status == ExecutionStatus.SUCCESS.value


async def test_approval_flow_reject(monkeypatch):
    """场景4: 审批拒绝, 工具不执行, 记录标记rejected"""
    tool = FakeTool("run_shell")
    patch_agent_deps(monkeypatch, ScriptedModel([
        AIMessage(content="", tool_calls=[tool_call("run_shell", {"command": "dir"}, "c1")]),
        AIMessage(content="好的, 已取消"),
    ]), tools={"run_shell": tool})
    sid = await create_user_and_session()
    await run_agent_session(sid, "列目录")
    reply = await resume_agent_session(await get_pending_approval_id(sid), ApprovalStatus.REJECTED)
    assert reply == "好的, 已取消"
    assert tool.calls == []
    assert (await get_executions(sid))[0].status == ExecutionStatus.REJECTED.value


async def test_multi_tool_message_inputs_recorded(monkeypatch):
    """场景5: 回归-同消息审批+非审批工具, 恢复后两者入参都完整落库"""
    shell = FakeTool("run_shell")
    search = FakeTool("web_search")
    patch_agent_deps(monkeypatch, ScriptedModel([
        AIMessage(content="", tool_calls=[
            tool_call("run_shell", {"command": "dir"}, "c1"),
            tool_call("web_search", {"query": "新闻"}, "c2"),
        ]),
        AIMessage(content="完成"),
    ]), tools={"run_shell": shell, "web_search": search})
    sid = await create_user_and_session()
    await run_agent_session(sid, "列目录并搜新闻")
    await resume_agent_session(await get_pending_approval_id(sid), ApprovalStatus.APPROVED)
    rows = {r.tool_name: r for r in await get_executions(sid)}
    assert rows["run_shell"].tool_input == {"command": "dir"}
    assert rows["web_search"].tool_input == {"query": "新闻"}


async def test_cancel_run_compensates(monkeypatch):
    """场景6: 运行中取消, 落库打断消息并置has_pending_task"""
    patch_agent_deps(monkeypatch, SlowModel(seconds=30))
    sid = await create_user_and_session()
    task = asyncio.create_task(run_agent_session(sid, "开始长任务"))
    await asyncio.sleep(0.3)  # 等图跑起来
    assert request_cancel_session(sid) is True
    with pytest.raises(RunCancelledError):
        await task
    msgs = await list_messages(sid)
    assert msgs[-1].role == MessageRole.SYSTEM.value
    assert msgs[-1].content == CANCEL_MESSAGE
    async with SessionLocal() as db:
        assert await sessions_crud.get_has_pending_task(db, sid) is True


async def test_cancel_queued_run_aborts(monkeypatch):
    """场景6b: 回归-排队中的任务被取消后放弃执行, 不顶替前一轮"""
    patch_agent_deps(monkeypatch, SlowModel(seconds=30))
    sid = await create_user_and_session()
    task1 = asyncio.create_task(run_agent_session(sid, "第一个"))
    await asyncio.sleep(0.2)
    task2 = asyncio.create_task(run_agent_session(sid, "第二个"))  # 排队等锁
    await asyncio.sleep(0.2)
    request_cancel_session(sid)
    with pytest.raises(RunCancelledError):
        await task1
    with pytest.raises(RunCancelledError):
        await task2
    msgs = await list_messages(sid)
    assert [m.content for m in msgs if m.role == MessageRole.USER.value] == ["第一个"]


async def test_retry_cleans_and_reruns(monkeypatch):
    """场景7: 重试清理旧回复与工具记录后重新运行"""
    tool = FakeTool("web_search", result="结果")
    patch_agent_deps(monkeypatch, ScriptedModel([
        AIMessage(content="", tool_calls=[tool_call("web_search", {"query": "旧问题"}, "c1")]),
        AIMessage(content="旧回答"),
        AIMessage(content="", tool_calls=[tool_call("web_search", {"query": "新问题"}, "c2")]),
        AIMessage(content="新回答"),
    ]), tools={"web_search": tool})
    sid = await create_user_and_session()
    first = await run_agent_session(sid, "旧问题")
    assert first.content == "旧回答"
    assert len(await get_executions(sid)) == 1
    user_msg_id = (await list_messages(sid))[0].id
    new_reply = await retry_agent_session(sid, user_msg_id, "新问题")
    assert new_reply.content == "新回答"
    assert [m.content for m in await list_messages(sid)] == ["新问题", "新回答"]
    rows = await get_executions(sid)
    assert len(rows) == 1  # 旧的执行记录已被清理
    assert rows[0].tool_input == {"query": "新问题"}


async def test_pending_task_injects_previous_plan(monkeypatch):
    """场景8: 回归-被打断的计划在下一轮注入图输入"""
    from uuid import uuid4

    captured = {}
    todo_id = str(uuid4())  # todos.id 是UUID列, 必须用合法UUID

    async def fake_process_stream(db, session_id, plan_queue, graph_input, config, cancel_event=None, **kwargs):
        captured["graph_input"] = graph_input
        return StreamResult(final_reply="检查完成", interrupt=None)

    monkeypatch.setattr("app.core.session_runner.runner.process_stream", fake_process_stream)
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="继续")]))
    sid = await create_user_and_session()
    async with SessionLocal() as db:
        await todos_crud.replace_todos(
            db, sid, [TodoItem(id=todo_id, title="老计划步骤", status=TodoStatus.PENDING, position=0)]
        )
        await sessions_crud.set_has_pending_task(db, sid, True)
        await db.commit()
    await run_agent_session(sid, "继续执行")
    injected = captured["graph_input"].get("todos")
    assert injected is not None
    assert injected[0].id == todo_id
