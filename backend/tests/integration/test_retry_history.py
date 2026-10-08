"""集成测试: 重试保留历史、编辑输入形成新版本、新运行关联源运行。

契约: 重试不删除任何消息/审批/工具记录; 编辑后的输入创建新的消息版本;
新运行通过 retry_of_run_id 关联源运行, attempt 沿重试链延续;
新尝试的上下文不包含上一次尝试的回复。
"""

from app.core.graph import builder
from app.core.session_runner import retry_agent_session, run_agent_session
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.models import Message, Run
from app.db.session import SessionLocal
from app.schemas.enums import MessageRole
from langchain_core.messages import AIMessage
from sqlalchemy import select
from tests.fakes import FakePlanner, ScriptedModel


class RecordingModel:
    """记录每次调用收到的消息列表, 再按预置顺序返回响应的假模型。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(list(messages))
        return self.responses.pop(0)


def patch_agent_deps(monkeypatch, model, tools=None):
    """替换全局图单例运行时的模型/计划器/工具"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: FakePlanner([]))
    monkeypatch.setattr(builder, "get_planner_structured_method", lambda: "function_calling")
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools or {})


async def create_user_and_session(username="retry_tester"):
    """创建用户与会话, 返回会话ID"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "重试会话", user.id)
        await db.commit()
        return session.id


async def get_runs(session_id):
    """按登记时间正序取会话运行记录"""
    async with SessionLocal() as db:
        result = await db.execute(select(Run).where(Run.session_id == session_id).order_by(Run.created_at, Run.id))
        return list(result.scalars())


async def list_messages(session_id):
    """按时间正序取会话消息"""
    async with SessionLocal() as db:
        result = await db.execute(
            select(Message).where(Message.session_id == session_id).order_by(Message.created_at, Message.id)
        )
        return list(result.scalars())


async def test_retry_preserves_history_and_creates_new_version(monkeypatch):
    """编辑重试: 旧消息原样保留, 新内容形成新的用户消息版本, 新运行关联源运行"""
    model = RecordingModel([AIMessage(content="旧回答"), AIMessage(content="新回答")])
    patch_agent_deps(monkeypatch, model)
    sid = await create_user_and_session()
    assert (await run_agent_session(sid, "旧问题")).content == "旧回答"

    msgs = await list_messages(sid)
    original_user_id = next(m for m in msgs if m.role == MessageRole.USER.value).id

    reply = await retry_agent_session(sid, original_user_id, "新问题")
    assert reply.content == "新回答"

    msgs = await list_messages(sid)
    # 旧消息一条没删: 旧用户 + 旧助手 + 新用户 + 新助手
    assert [(m.role, m.content) for m in msgs] == [
        (MessageRole.USER.value, "旧问题"),
        (MessageRole.ASSISTANT.value, "旧回答"),
        (MessageRole.USER.value, "新问题"),
        (MessageRole.ASSISTANT.value, "新回答"),
    ]

    runs = await get_runs(sid)
    assert len(runs) == 2
    assert runs[0].input_message_id == original_user_id  # 旧运行仍指向旧输入版本
    assert runs[1].input_message_id != original_user_id  # 新运行指向新消息版本
    assert runs[1].retry_of_run_id == runs[0].id  # 新运行关联源运行
    assert runs[1].attempt == 2  # attempt 沿重试链延续

    # 新尝试的上下文不包含上一次尝试的回复
    retry_call = model.calls[-1]
    assert all("旧回答" not in (m.content or "") for m in retry_call)
    assert any("新问题" in (m.content or "") for m in retry_call)


async def test_retry_without_edit_reuses_input_and_increments_attempt(monkeypatch):
    """不编辑重试: 复用同一条输入消息, attempt 递增, 不产生新消息版本"""
    patch_agent_deps(monkeypatch, ScriptedModel([AIMessage(content="旧回答"), AIMessage(content="新回答")]))
    sid = await create_user_and_session()
    await run_agent_session(sid, "旧问题")
    user_id = next(m for m in await list_messages(sid) if m.role == MessageRole.USER.value).id

    reply = await retry_agent_session(sid, user_id, None)
    assert reply.content == "新回答"

    msgs = await list_messages(sid)
    # 不产生新的用户消息: 旧用户 + 旧助手 + 新助手
    assert [m.role for m in msgs] == [
        MessageRole.USER.value,
        MessageRole.ASSISTANT.value,
        MessageRole.ASSISTANT.value,
    ]
    runs = await get_runs(sid)
    assert runs[1].input_message_id == user_id  # 复用同一条输入
    assert runs[1].attempt == 2
    assert runs[1].retry_of_run_id == runs[0].id
