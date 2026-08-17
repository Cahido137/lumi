"""WebSocket 路由相关"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, WebSocketException

from app.core.event_bus import event_bus
from app.core.session_runner import run_agent_session


router = APIRouter(prefix="/api/ws", tags=["WebSocket"])


async def _receive_loop(websocket: WebSocket, session_id: str) -> None:
    """接收消息并在指定会话开启一轮 Agent"""
    while True:
        data = await websocket.receive_json()  # 等待消息
        content = data.get("content", "").strip()
        if not content:
            continue
        asyncio.create_task(run_agent_session(session_id, content))

async def _send_loop(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """从事件总线推送消息"""
    while True:
        event = await queue.get()  # 从队列取事件
        await websocket.send_json(event.model_dump(mode="json", by_alias=True))


@router.websocket("/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: UUID):
    """实时聊天"""
    sid = str(session_id)
    # 握手连接
    await websocket.accept()
    # 订阅事件
    queue = event_bus.subscribe(sid)

    try:
        # 同时开始收发任务
        recv_task = asyncio.create_task(_receive_loop(websocket, sid))
        send_task = asyncio.create_task(_send_loop(websocket, queue))
        await asyncio.gather(recv_task, send_task)
    except WebSocketDisconnect:
        pass
    except Exception:
        raise WebSocketException(code=1011, reason="服务器内部错误")
    finally:
        event_bus.unsubscribe(sid)  # 取消订阅