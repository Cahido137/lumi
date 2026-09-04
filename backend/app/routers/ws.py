"""WebSocket 路由相关"""

import asyncio
import logging
from uuid import UUID

import jwt as pyjwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, WebSocketException

from app.core.event_bus import event_bus
from app.core.session_runner import request_cancel_session, run_agent_session
from app.crud import sessions as sessions_crud
from app.crud import users as users_crud
from app.db.session import SessionLocal
from app.utils.security import decode_access_token

router = APIRouter(prefix="/api/ws", tags=["WebSocket"])

logger = logging.getLogger(__name__)


async def _send_loop(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """从事件总线推送消息"""
    while True:
        event = await queue.get()  # 从队列取事件
        await websocket.send_json(event.model_dump(mode="json", by_alias=True))


@router.websocket("/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: UUID):
    """实时聊天"""
    sid = str(session_id)
    token = websocket.query_params.get("token")
    try:
        uid: int = decode_access_token(token)
    except (pyjwt.PyJWTError, AttributeError, ValueError):
        await websocket.close(code=4401)  # 令牌校验未通过关闭连接
        return

    async with SessionLocal() as db:
        user = await users_crud.get_user_by_uid(db, uid)
        if user is None:
            await websocket.close(code=4401)
            return
        session = await sessions_crud.get_session_for_user(db, sid, user.id)
        if session is None:
            await websocket.close(code=4404)  # 会话不存在或无权限
            return

    # 握手连接
    await websocket.accept()
    logger.info("WebSocket建立连接 (session_id=%s)", sid)
    # 订阅事件
    queue = event_bus.subscribe(sid)

    # 已创建的运行任务集合
    tasks: set[asyncio.Task] = set()

    def _on_run_done(task: asyncio.Task) -> None:
        """运行结束清理任务引用并取出可能的异常"""
        tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()  # 取出异常
            if exc is not None:
                logger.error("会话运行任务异常 (session_id=%s)", sid, exc_info=exc)

    try:
        send_task = asyncio.create_task(_send_loop(websocket, queue))
        tasks.add(send_task)
        while True:
            data = await websocket.receive_json()  # 接收数据
            if not isinstance(data, dict):
                continue
            content = str(data.get("content") or "").strip()  # 清洗数据
            if not content:
                continue
            run_task = asyncio.create_task(run_agent_session(sid, content))
            tasks.add(run_task)
            run_task.add_done_callback(_on_run_done)
    except WebSocketDisconnect:
        logger.info("WebSocket连接断开 (session_id=%s)", sid)
    except Exception as e:
        logger.exception("WebSocket处理异常 (session_id=%s)", sid)
        raise WebSocketException(code=1011, reason="服务器内部错误") from e
    finally:
        request_cancel_session(sid)
        send_task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event_bus.unsubscribe(sid, queue)
