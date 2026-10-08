"""集成测试: 工具事件的发布时机与事实对齐。

契约: 模型提出工具调用只发 tool_requested; 真正开始执行才发 tool_started。
等待审批期间只有 requested 与 approval_required, 没有 started。
"""

from app.core.event_bus import event_bus
from app.core.graph import builder
from app.core.session_runner import resume_agent_session, run_agent_session
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.models import Approval
from app.db.session import SessionLocal
from app.schemas.enums import ApprovalStatus, EventType
from langchain_core.messages import AIMessage
from sqlalchemy import select
from tests.fakes import FakePlanner, FakeTool, ScriptedModel


def test_tool_event_type_values():
    """事件枚举值即前端订阅名, 不能随意改动"""
    assert EventType.TOOL_REQUESTED.value == "tool_requested"
    assert EventType.TOOL_STARTED.value == "tool_started"
    assert EventType.TOOL_FINISHED.value == "tool_finished"


def tool_call(name, args, call_id):
    """构造模型工具调用"""
    return {"name": name, "args": args, "id": call_id}


def patch_agent_deps(monkeypatch, model, tools=None):
    """替换全局图单例运行时的模型/计划器/工具"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: FakePlanner([]))
    monkeypatch.setattr(builder, "get_planner_structured_method", lambda: "function_calling")
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools or {})


async def create_user_and_session(username="evt_tester"):
    """创建用户与会话, 返回会话ID"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "事件会话", user.id)
        await db.commit()
        return session.id


async def get_pending_approval_id(session_id):
    """取会话的待审批单ID"""
    async with SessionLocal() as db:
        return await db.scalar(select(Approval.id).where(Approval.session_id == session_id))


def drain(queue):
    """取出队列里的全部事件, 保持入队顺序"""
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def test_requested_precedes_started_for_non_approval_tool(monkeypatch):
    """无需审批的工具: 先 requested 后 started, 两者带同一 tool_call_id"""
    tool = FakeTool("web_search", result="3条结果")
    patch_agent_deps(
        monkeypatch,
        ScriptedModel(
            [
                AIMessage(content="", tool_calls=[tool_call("web_search", {"query": "新闻"}, "c1")]),
                AIMessage(content="搜索完成"),
            ]
        ),
        tools={"web_search": tool},
    )
    sid = await create_user_and_session()
    queue = event_bus.subscribe(sid)
    reply = await run_agent_session(sid, "搜新闻")
    assert reply.content == "搜索完成"

    events = drain(queue)
    types = [e.event_type for e in events]
    assert types.index(EventType.TOOL_REQUESTED) < types.index(EventType.TOOL_STARTED)
    requested = next(e for e in events if e.event_type == EventType.TOOL_REQUESTED)
    started = next(e for e in events if e.event_type == EventType.TOOL_STARTED)
    # requested 与 started 用同一 tool_call_id 关联, 前端据此把两阶段串起来
    assert requested.data.tool_call_id == "c1"
    assert started.data.tool_call_id == "c1"


async def test_no_started_until_approved(monkeypatch):
    """审批工具: 提出调用与等待审批期间只有 requested, 批准执行后才发 started"""
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
    sid = await create_user_and_session()
    queue = event_bus.subscribe(sid)
    assert await run_agent_session(sid, "列目录") is None  # 中断等待审批

    types_before = [e.event_type for e in drain(queue)]
    assert EventType.TOOL_REQUESTED in types_before  # 模型提出了调用
    assert EventType.APPROVAL_REQUIRED in types_before  # 在等待审批
    assert EventType.TOOL_STARTED not in types_before  # 审批前不得宣称已开始

    reply = await resume_agent_session(await get_pending_approval_id(sid), ApprovalStatus.APPROVED)
    assert reply == "执行完毕"
    types_after = [e.event_type for e in drain(queue)]
    assert EventType.TOOL_STARTED in types_after  # 批准后真正执行才发 started
