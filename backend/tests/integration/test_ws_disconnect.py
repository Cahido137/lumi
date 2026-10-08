"""集成测试: WebSocket 连接断开不再取消正在运行的 Run。

契约(ADR-0001): 订阅连接的生命周期与 Run 的生命周期互相独立。
断开只结束订阅与推送; 取消必须通过 REST 取消接口显式发出。
"""

from app.core.graph import builder
from app.core.session_runner.state import CANCEL_MESSAGE
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.models import Message, Run
from app.db.session import SessionLocal
from app.routers.ws import websocket_chat
from app.schemas.enums import MessageRole, RunStatus
from app.utils.security import create_access_token
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect
from tests.fakes import FakePlanner, SlowModel


class FakeWebSocket:
    """脚本化的 WebSocket 替身: 按脚本返回接收数据, 记录发送内容。

    Note:
        只实现 websocket_chat 用到的接口面, 不是完整协议替身。
    """

    def __init__(self, token: str, receive_script: list):
        self.query_params = {"token": token}
        self._receive = list(receive_script)
        self.sent: list[dict] = []
        self.closed_code: int | None = None
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int | None = None) -> None:
        self.closed_code = code

    async def receive_json(self) -> dict:
        if not self._receive:
            raise WebSocketDisconnect()
        item = self._receive.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def patch_agent_deps(monkeypatch, model, tools=None):
    """替换全局图单例运行时的模型/计划器/工具"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: FakePlanner([]))
    monkeypatch.setattr(builder, "get_planner_structured_method", lambda: "function_calling")
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools or {})


async def create_user_and_session(username="ws_tester"):
    """创建用户与会话, 返回(用户uid, 会话ID)。

    Note:
        JWT 的 sub 声明携带整数 uid(users.uid), 不是 UUID 主键 users.id。
    """
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "WS会话", user.id)
        await db.commit()
        return user.uid, session.id


async def get_run(session_id):
    """取会话的运行记录"""
    async with SessionLocal() as db:
        result = await db.execute(select(Run).where(Run.session_id == session_id))
        return result.scalars().first()


async def list_messages(session_id):
    """按时间正序取会话消息"""
    async with SessionLocal() as db:
        result = await db.execute(
            select(Message).where(Message.session_id == session_id).order_by(Message.created_at, Message.id)
        )
        return list(result.scalars())


async def test_ws_disconnect_does_not_cancel_run(monkeypatch):
    """断开发生在运行中: Run 继续走到成功, 不被当作取消处理"""
    patch_agent_deps(monkeypatch, SlowModel(seconds=1, content="断开后继续完成"))
    uid, sid = await create_user_and_session()

    ws = FakeWebSocket(
        token=create_access_token(uid),
        receive_script=[{"content": "你好"}, WebSocketDisconnect()],
    )
    # 处理器在 finally 中等待运行任务收尾后才返回, 因此返回时运行已结束
    await websocket_chat(ws, sid)

    run = await get_run(sid)
    assert run is not None
    assert run.status == RunStatus.SUCCEEDED.value  # 断开没有把运行改成取消

    msgs = await list_messages(sid)
    assert all(m.content != CANCEL_MESSAGE for m in msgs)  # 没有插入打断消息
    assistant = [m for m in msgs if m.role == MessageRole.ASSISTANT.value]
    assert assistant and assistant[-1].content == "断开后继续完成"
