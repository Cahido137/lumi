"""E2E测试: 取消端点登记运行记录的打断请求时刻。"""

import asyncio

import httpx
import pytest
from app.core.graph import builder
from app.core.session_runner import RunCancelledError, resume_agent_session, run_agent_session
from app.db.models import Approval, Run
from app.db.session import SessionLocal
from app.main import app
from app.schemas.enums import ApprovalStatus, RunStatus
from app.utils.errors import ConflictError
from langchain_core.messages import AIMessage
from sqlalchemy import select
from tests.fakes import FakePlanner, FakeTool, ScriptedModel, SlowModel


def patch_agent_deps(monkeypatch, model, tools=None):
    """替换全局图单例运行时的模型/计划器/工具"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: FakePlanner([]))
    monkeypatch.setattr(builder, "get_planner_structured_method", lambda: "function_calling")
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools or {})


@pytest.fixture()
async def client():
    """ASGI传输: 直接调app对象, 不经过网络"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def auth_header(token_data):
    """构造登录后的请求头"""
    return {"Authorization": f"Bearer {token_data['accessToken']}"}


async def register_user(client, username="cancel_user"):
    """注册用户并返回令牌载荷"""
    res = await client.post("/api/auth/register", json={"username": username, "password": "pass1234"})
    assert res.status_code == 200
    return res.json()["data"]


async def create_session(client, token_data):
    """创建一个会话并返回会话ID"""
    res = await client.post("/api/sessions/create", json={"title": "取消会话"}, headers=auth_header(token_data))
    assert res.status_code == 200
    return res.json()["data"]["id"]


async def get_pending_approval(session_id) -> Approval | None:
    """取会话中尚未决定的审批单"""
    async with SessionLocal() as db:
        return await db.scalar(
            select(Approval).where(Approval.session_id == session_id, Approval.status == ApprovalStatus.PENDING.value)
        )


async def get_runs(session_id) -> list[Run]:
    """取会话全部运行记录"""
    async with SessionLocal() as db:
        result = await db.execute(select(Run).where(Run.session_id == session_id))
        return list(result.scalars())


async def test_cancel_endpoint_records_request_then_cancels_run(client, monkeypatch):
    """取消端点先登记请求时刻, 运行随后收尾为 cancelled"""
    patch_agent_deps(monkeypatch, SlowModel(seconds=30))
    token = await register_user(client)
    sid = await create_session(client, token)
    task = asyncio.create_task(run_agent_session(sid, "长任务"))
    await asyncio.sleep(0.3)  # 等运行登记完成并进入 running

    res = await client.post(f"/api/sessions/{sid}/cancel", headers=auth_header(token))
    assert res.status_code == 200
    assert res.json()["data"]["cancelled"] is True
    with pytest.raises(RunCancelledError):
        await task

    runs = await get_runs(sid)
    assert len(runs) == 1
    run = runs[0]
    assert run.status == RunStatus.CANCELLED
    assert run.cancel_requested_at is not None
    assert run.finished_at is not None
    # 请求到达的时刻必须早于运行真正停止的时刻, 两者分开才有诊断价值
    assert run.finished_at >= run.cancel_requested_at
    assert run.error_code is None


async def test_cancel_endpoint_without_active_run_changes_nothing(client):
    """没有运行中的对话时返回 cancelled=false, 也不产生运行记录"""
    token = await register_user(client, "cancel_idle")
    sid = await create_session(client, token)
    res = await client.post(f"/api/sessions/{sid}/cancel", headers=auth_header(token))
    assert res.status_code == 200
    body = res.json()
    assert body["data"]["cancelled"] is False
    assert await get_runs(sid) == []


async def test_cancel_waiting_approval_terminates_run_and_invalidates_approval(client, monkeypatch):
    """等待审批时取消: 运行进终态, 待决审批失效, 事后再批准不会启动执行"""
    tool_call = {"name": "run_shell", "args": {"command": "dir"}, "id": "c1"}
    patch_agent_deps(
        monkeypatch,
        ScriptedModel([AIMessage(content="", tool_calls=[tool_call])]),
        tools={"run_shell": FakeTool("run_shell", result="目录列表")},
    )
    token = await register_user(client, "cancel_wait")
    sid = await create_session(client, token)
    assert await run_agent_session(sid, "列目录") is None
    assert (await get_runs(sid))[0].status == RunStatus.WAITING_APPROVAL
    approval = await get_pending_approval(sid)
    assert approval is not None

    res = await client.post(f"/api/sessions/{sid}/cancel", headers=auth_header(token))
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["cancelled"] is True
    assert data["status"] == RunStatus.CANCELLED.value

    run = (await get_runs(sid))[0]
    assert run.status == RunStatus.CANCELLED
    assert run.finished_at is not None
    assert run.error_code is None

    # 尚未决定的审批失效; 取消不是人工决定, 因此不写 decided_at
    async with SessionLocal() as db:
        after = await db.get(Approval, approval.id)
    assert after.status == ApprovalStatus.CANCELLED.value
    assert after.decided_at is None

    # 事后再批准不得启动执行: 空脚本模型若被调用会抛 IndexError, 而不是这里的冲突
    patch_agent_deps(monkeypatch, ScriptedModel([]))
    with pytest.raises(ConflictError, match="审批单已失效"):
        await resume_agent_session(approval.id, ApprovalStatus.APPROVED)
    assert (await get_runs(sid))[0].status == RunStatus.CANCELLED
