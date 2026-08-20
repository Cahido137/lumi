"""WebSocket 路由相关"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, WebSocketException

from app.core.event_bus import event_bus
from app.core.session_runner import run_agent_session


router = APIRouter(prefix="/api/ws", tags=["WebSocket"])


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

    # 已创建的运行任务集合
    tasks: set[asyncio.Task] = set()

    def _on_run_done(task: asyncio.Task) -> None:
        """运行结束清理任务引用并取出可能的异常"""
        tasks.discard(task)
        if not task.cancelled():
            task.exception()  # 取出异常

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
        pass
    except Exception:
        raise WebSocketException(code=1011, reason="服务器内部错误")
    finally:
        # 断开连接后取消所有运行中的任务释放资源
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event_bus.unsubscribe(sid)