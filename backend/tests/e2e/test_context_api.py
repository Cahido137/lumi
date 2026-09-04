"""E2E测试: 上下文管理路由(用量查询与手动压缩), 不触碰真实大模型"""

from types import SimpleNamespace
from uuid import uuid4

import app.routers.context as context_router
import httpx
import pytest
from app.core.prompts import get_system_messages
from app.crud import messages as messages_crud
from app.crud import sessions as sessions_crud
from app.db.session import SessionLocal
from app.main import app
from app.schemas.enums import MessageRole
from langchain_core.messages import HumanMessage

# ---------- 测试工具 ----------


@pytest.fixture()
async def client():
    """ASGI传输: 直接调app对象, 不经过网络"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def auth_header(token_data):
    return {"Authorization": f"Bearer {token_data['accessToken']}"}


async def register_user(client, username="ctx_user"):
    res = await client.post("/api/auth/register", json={"username": username, "password": "pass1234"})
    assert res.status_code == 200
    return res.json()["data"]


async def create_session(client, token_data):
    res = await client.post("/api/sessions/create", json={"title": "上下文会话"}, headers=auth_header(token_data))
    assert res.status_code == 200
    return res.json()["data"]["id"]


async def insert_messages(session_id, count=4) -> list[str]:
    """直接向数据库插入交替的用户/助手消息(绕过图), 返回消息id字符串列表"""
    contents = ["你好", "你好, 有什么可以帮你", "给我讲个故事", "从前有座山"]
    async with SessionLocal() as db:
        for i in range(count):
            role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
            await messages_crud.add_message(db, session_id, role, contents[i])
        await db.commit()
        rows = await messages_crud.list_message_asc(db, session_id)
        return [str(row.id) for row in rows]


def _patch_limits(monkeypatch):
    """替换路由模块内的阈值解析, 避免读真实配置, 数值可断言"""
    monkeypatch.setattr(
        context_router,
        "get_compact_limits",
        lambda: SimpleNamespace(
            max_context_tokens=100_000,
            warn_tokens=60_000,
            trigger_tokens=75_000,
        ),
    )


class _FakeNoopMiddleware:
    """中间件替身: abefore_model返回None表示判定无需压缩"""

    async def abefore_model(self, state, runtime):
        return None


# ---------- GET /context/usage ----------


async def test_usage_requires_auth(client):
    """未登录查询用量返回401"""
    data = await register_user(client)
    sid = await create_session(client, data)
    res = await client.get(f"/api/sessions/{sid}/context/usage")
    assert res.status_code == 401


async def test_usage_rejects_other_user_session(client):
    """不能访问他人会话, 查询与压缩都返回404"""
    owner = await register_user(client, "ctx_owner")
    other = await register_user(client, "ctx_other")
    sid = await create_session(client, owner)
    res = await client.get(f"/api/sessions/{sid}/context/usage", headers=auth_header(other))
    assert res.status_code == 404
    res2 = await client.post(f"/api/sessions/{sid}/context/compact", headers=auth_header(other))
    assert res2.status_code == 404


async def test_usage_empty_session(client, monkeypatch):
    """空会话: 用量只来自系统提示词, 阈值原样透出"""
    _patch_limits(monkeypatch)
    data = await register_user(client)
    sid = await create_session(client, data)
    res = await client.get(f"/api/sessions/{sid}/context/usage", headers=auth_header(data))
    assert res.status_code == 200
    d = res.json()["data"]
    assert d["messageCount"] == len(get_system_messages())  # 只有系统提示词
    assert d["usedTokens"] > 0
    assert d["maxContextTokens"] == 100_000
    assert d["warnTokens"] == 60_000
    assert d["triggerTokens"] == 75_000
    assert d["compacted"] is False
    assert d["fraction"] == round(d["usedTokens"] / 100_000, 4)


async def test_usage_grows_with_history(client, monkeypatch):
    """历史消息入库后, 用量与消息数同步增长"""
    _patch_limits(monkeypatch)
    data = await register_user(client)
    sid = await create_session(client, data)
    before = await client.get(f"/api/sessions/{sid}/context/usage", headers=auth_header(data))
    assert before.status_code == 200
    await insert_messages(sid, 4)
    after = await client.get(f"/api/sessions/{sid}/context/usage", headers=auth_header(data))
    assert after.status_code == 200
    b, a = before.json()["data"], after.json()["data"]
    assert a["messageCount"] == b["messageCount"] + 4
    assert a["usedTokens"] > b["usedTokens"]


# ---------- POST /context/compact 守卫分支 ----------


async def test_compact_rejects_running_session(client, monkeypatch):
    """会话正在运行时手动压缩直接409"""
    data = await register_user(client)
    sid = await create_session(client, data)
    monkeypatch.setattr(context_router, "is_session_running", lambda _: True)
    res = await client.post(f"/api/sessions/{sid}/context/compact", headers=auth_header(data))
    assert res.status_code == 409
    assert "正在运行" in res.json()["message"]


async def test_compact_empty_session_returns_zero(client, monkeypatch):
    """空会话只有系统提示词, 无可压缩内容: 返回200与零压缩结果"""
    _patch_limits(monkeypatch)
    data = await register_user(client)
    sid = await create_session(client, data)
    # 替换中间件工厂, 避免构造真实模型; 空会话走的是run_compaction的真实"无可压缩"分支
    monkeypatch.setattr(context_router, "get_manual_compact_middleware", lambda: _FakeNoopMiddleware())
    res = await client.post(f"/api/sessions/{sid}/context/compact", headers=auth_header(data))
    assert res.status_code == 200
    d = res.json()["data"]
    assert d["summarizedMessageCount"] == 0
    assert d["beforeTokens"] == d["afterTokens"] > 0


# ---------- POST /context/compact 压缩主流程 ----------


async def test_compact_success_persists_summary(client, monkeypatch):
    """压缩成功: 摘要与边界落库, 原消息不删除, 用量查询反映已压缩状态"""
    _patch_limits(monkeypatch)
    data = await register_user(client)
    sid = await create_session(client, data)
    ids = await insert_messages(sid, 4)
    covered, kept = ids[:2], ids[2:]
    summary_text = "以下是早前对话的摘要"

    async def fake_run_compaction(middleware, messages):
        """模拟一次成功压缩: 前两条被摘要, 后两条保留"""
        summary = HumanMessage(content=summary_text, id="summary-1")
        outcome = SimpleNamespace(
            summary_message=summary,
            preserved_messages=[],
            covered_ids=covered,
        )
        return [summary], outcome

    monkeypatch.setattr(context_router, "get_manual_compact_middleware", lambda: SimpleNamespace())
    monkeypatch.setattr(context_router, "run_compaction", fake_run_compaction)

    res = await client.post(f"/api/sessions/{sid}/context/compact", headers=auth_header(data))
    assert res.status_code == 200
    d = res.json()["data"]
    assert d["summarizedMessageCount"] == 2
    assert d["afterTokens"] < d["beforeTokens"]

    # 摘要与压缩边界已落库, 且原消息不被删除
    async with SessionLocal() as db:
        text, until_id = await sessions_crud.get_context_summary(db, sid)
        assert text == summary_text
        assert until_id == covered[-1]
        rows = await messages_crud.list_message_asc(db, sid)
        assert len(rows) == 4

    # 用量查询反映压缩状态: 系统提示词 + 1条摘要 + 2条保留消息
    usage = await client.get(f"/api/sessions/{sid}/context/usage", headers=auth_header(data))
    assert usage.status_code == 200
    u = usage.json()["data"]
    assert u["compacted"] is True
    assert u["messageCount"] == len(get_system_messages()) + 1 + len(kept)


async def test_compact_llm_failure_returns_500(client, monkeypatch):
    """压缩内部(摘要模型调用)异常转为友好的500"""
    data = await register_user(client)
    sid = await create_session(client, data)
    await insert_messages(sid, 2)

    async def fake_run_compaction(middleware, messages):
        raise RuntimeError("模型服务不可用")

    monkeypatch.setattr(context_router, "get_manual_compact_middleware", lambda: SimpleNamespace())
    monkeypatch.setattr(context_router, "run_compaction", fake_run_compaction)
    res = await client.post(f"/api/sessions/{sid}/context/compact", headers=auth_header(data))
    assert res.status_code == 500
    assert "上下文压缩失败" in res.json()["message"]


async def test_compact_validation_failure_writes_nothing(client, monkeypatch):
    """覆盖消息未全部落库时拒绝持久化: 返回500且不写入脏摘要"""
    data = await register_user(client)
    sid = await create_session(client, data)
    await insert_messages(sid, 2)
    ghost_id = str(uuid4())  # 数据库中不存在的消息id

    async def fake_run_compaction(middleware, messages):
        summary = HumanMessage(content="摘要", id="summary-x")
        outcome = SimpleNamespace(
            summary_message=summary,
            preserved_messages=[],
            covered_ids=[ghost_id],
        )
        return [summary], outcome

    monkeypatch.setattr(context_router, "get_manual_compact_middleware", lambda: SimpleNamespace())
    monkeypatch.setattr(context_router, "run_compaction", fake_run_compaction)

    res = await client.post(f"/api/sessions/{sid}/context/compact", headers=auth_header(data))
    assert res.status_code == 500
    assert "校验失败" in res.json()["message"]

    # 数据库中不能留下任何摘要记录
    async with SessionLocal() as db:
        text, until_id = await sessions_crud.get_context_summary(db, sid)
        assert text is None
        assert until_id is None
