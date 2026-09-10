"""WebSocket 路由相关。"""

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
    """把事件总线队列中的事件逐条推送给 WebSocket 客户端。

    Args:
        websocket: 已握手的 WebSocket 连接。
        queue: 该连接的事件订阅队列。
    """
    while True:
        event = await queue.get()  # 从队列取事件
        await websocket.send_json(event.model_dump(mode="json", by_alias=True))


@router.websocket("/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: UUID):
    """建立指定会话的 WebSocket 事件流。

    Note:
        JWT 通过 query 参数 token 传递而不是 Authorization 头。
        连接建立后, 客户端发送的每一条文本消息都会触发一轮 Agent 运行。
        连接断开后, 会取消该会话正在运行的任务。

        关闭码:
            4401: 缺少 token、token 校验失败或用户不存在。
            4404: 会话不存在或不属于该用户。
    """
    sid = str(session_id)
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401)
        return
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
