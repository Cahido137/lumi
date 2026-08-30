"""E2E测试: HTTP接口链(注册→会话→聊天→审批), 模型用假的, 不起真实服务器"""
import asyncio

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.core.graph import builder
from app.db.models import Approval, ToolExecution
from app.db.session import SessionLocal
from app.main import app
from app.schemas.enums import ExecutionStatus
from tests.fakes import FakePlanner, FakeTool, ScriptedModel, SlowModel


def patch_agent_deps(monkeypatch, model, tools=None):
    """替换全局图单例运行时的模型/计划器/工具"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: FakePlanner([]))
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools or {})


@pytest.fixture()
async def client():
    """ASGI传输: 直接调app对象, 不经过网络"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def auth_header(token_data):
    return {"Authorization": f"Bearer {token_data['accessToken']}"}


async def register_user(client, username="e2e_user"):
    res = await client.post("/api/auth/register", json={"username": username, "password": "pass1234"})
    assert res.status_code == 200
    return res.json()["data"]


async def create_session(client, token_data):
    res = await client.post("/api/sessions/create", json={"title": "E2E会话"}, headers=auth_header(token_data))
    assert res.status_code == 200
    return res.json()["data"]["id"]


async def get_approval_id(session_id):
    async with SessionLocal() as db:
        return await db.scalar(select(Approval.id).where(Approval.session_id == session_id))


async def get_executions(session_id):
    async with SessionLocal() as db:
        result = await db.execute(select(ToolExecution).where(ToolExecution.session_id == session_id))
        return list(result.scalars())


async def test_register_login_me_chain(client):
    """注册→登录→me 全链路, 以及401错误分支"""
    data = await register_user(client)
    assert data["user"]["username"] == "e2e_user"
    login = await client.post("/api/auth/login", json={"username": "e2e_user", "password": "pass1234"})
    assert login.status_code == 200
    me = await client.get("/api/auth/me", headers=auth_header(login.json()["data"]))
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "e2e_user"
    bad = await client.post("/api/auth/login", json={"username": "e2e_user", "password": "wrongpass"})
    assert bad.status_code == 401
    no_token = await client.get("/api/auth/me")
    assert no_token.status_code == 401


async def test_create_and_list_sessions(client):
    """建会话→列表可见"""
    data = await register_user(client)
    sid = await create_session(client, data)
    res = await client.get("/api/sessions/list", headers=auth_header(data))
    assert res.status_code == 200
    assert any(item["id"] == sid for item in res.json()["data"]["items"])


async def test_chat_normal_via_http(client, monkeypatch):
    """HTTP聊天: 假模型直接回答, 消息落库可查"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="你好, 我是助手")]))
    data = await register_user(client)
    sid = await create_session(client, data)
    res = await client.post(f"/api/sessions/{sid}/chat", json={"content": "你好"}, headers=auth_header(data))
    assert res.status_code == 200
    assert res.json()["data"]["reply"] == "你好, 我是助手"
    msgs = await client.get(f"/api/sessions/{sid}/messages", headers=auth_header(data))
    contents = [m["content"] for m in msgs.json()["data"]["items"]][::-1]  # 接口倒序返回, 反转为时间正序
    assert contents == ["你好", "你好, 我是助手"]


async def test_chat_records_tool_execution(client, monkeypatch):
    """HTTP聊天触发非审批工具, 执行记录落库"""
    tool = FakeTool("web_search", result="3条结果")
    patch_agent_deps(monkeypatch, ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "web_search", "args": {"query": "新闻"}, "id": "c1"}]),
        AIMessage(content="搜索完成"),
    ]), tools={"web_search": tool})
    data = await register_user(client)
    sid = await create_session(client, data)
    res = await client.post(f"/api/sessions/{sid}/chat", json={"content": "搜新闻"}, headers=auth_header(data))
    assert res.json()["data"]["reply"] == "搜索完成"
    rows = await get_executions(sid)
    assert len(rows) == 1
    assert rows[0].tool_input == {"query": "新闻"}


async def test_approval_flow_via_http(client, monkeypatch):
    """HTTP审批流: 聊天中断→决定批准→恢复执行"""
    tool = FakeTool("run_shell", result="目录列表")
    patch_agent_deps(monkeypatch, ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "run_shell", "args": {"command": "dir"}, "id": "c1"}]),
        AIMessage(content="执行完毕"),
    ]), tools={"run_shell": tool})
    data = await register_user(client)
    sid = await create_session(client, data)
    res = await client.post(f"/api/sessions/{sid}/chat", json={"content": "列目录"}, headers=auth_header(data))
    assert res.json()["message"] == "任务暂停, 等待人工审批"
    res2 = await client.post(
        f"/api/approvals/{await get_approval_id(sid)}/decide",
        json={"status": "approved", "scope": "one_time"},
        headers=auth_header(data),
    )
    assert res2.json()["message"] == "已收到决定, 任务已完成"
    rows = await get_executions(sid)
    assert rows[0].status == ExecutionStatus.SUCCESS.value
    assert tool.calls == [{"command": "dir"}]


async def test_approval_reject_via_http(client, monkeypatch):
    """HTTP审批流: 决定拒绝, 工具不执行, 记录rejected"""
    tool = FakeTool("run_shell")
    patch_agent_deps(monkeypatch, ScriptedModel([
        AIMessage(content="", tool_calls=[{"name": "run_shell", "args": {"command": "dir"}, "id": "c1"}]),
        AIMessage(content="好的, 已取消"),
    ]), tools={"run_shell": tool})
    data = await register_user(client)
    sid = await create_session(client, data)
    await client.post(f"/api/sessions/{sid}/chat", json={"content": "列目录"}, headers=auth_header(data))
    await client.post(
        f"/api/approvals/{await get_approval_id(sid)}/decide",
        json={"status": "rejected"},
        headers=auth_header(data),
    )
    rows = await get_executions(sid)
    assert rows[0].status == ExecutionStatus.REJECTED.value
    assert tool.calls == []


async def test_retry_via_http(client, monkeypatch):
    """HTTP重试: 编辑内容后重新回答"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="旧回答"), AIMessage(content="新回答")]))
    data = await register_user(client)
    sid = await create_session(client, data)
    res = await client.post(f"/api/sessions/{sid}/chat", json={"content": "旧问题"}, headers=auth_header(data))
    assert res.json()["data"]["reply"] == "旧回答"
    msgs = await client.get(f"/api/sessions/{sid}/messages", headers=auth_header(data))
    user_msg_id = next(m["id"] for m in msgs.json()["data"]["items"] if m["role"] == "user")
    res2 = await client.post(
        f"/api/sessions/{sid}/messages/{user_msg_id}/retry",
        json={"content": "新问题"},
        headers=auth_header(data),
    )
    assert res2.json()["data"]["reply"] == "新回答"
    msgs2 = await client.get(f"/api/sessions/{sid}/messages", headers=auth_header(data))
    assert [m["content"] for m in msgs2.json()["data"]["items"]][::-1] == ["新问题", "新回答"]


async def test_session_ownership_isolated(client):
    """会话归属: 访问他人会话返回404"""
    user_a = await register_user(client, "user_a")
    user_b = await register_user(client, "user_b")
    sid = await create_session(client, user_a)
    res = await client.get(f"/api/sessions/{sid}/messages", headers=auth_header(user_b))
    assert res.status_code == 404


async def test_cancel_via_http(client, monkeypatch):
    """HTTP取消: 长任务运行中打断, 聊天请求返回打断提示"""
    patch_agent_deps(monkeypatch, SlowModel(seconds=30))
    data = await register_user(client)
    sid = await create_session(client, data)
    chat_task = asyncio.create_task(
        client.post(f"/api/sessions/{sid}/chat", json={"content": "长任务"}, headers=auth_header(data))
    )
    await asyncio.sleep(0.3)
    res = await client.post(f"/api/sessions/{sid}/cancel", headers=auth_header(data))
    assert res.json()["data"]["cancelled"] is True
    chat_res = await chat_task
    assert chat_res.status_code == 200
    assert chat_res.json()["message"] == "[对话已被用户打断]"
