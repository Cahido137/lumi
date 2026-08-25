"""事件总线"""

import asyncio

from app.core.events import AgentEvent


MAX_QUEUE_SIZE = 1000

class EventBus:
    """事件总线类"""
    def __init__(self):
        # 会话事件队列
        self._queues: dict[str, list[asyncio.Queue]] = {}  # 设计多个队列防止资源竞争

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """消费者订阅会话事件流"""
        queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._queues.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        """消费者取消订阅会话事件流"""
        # 从队列列表中移除这个连接所占有的队列，防止影响其他连接
        queues = self._queues.get(session_id)
        if not queues:
            return
        try:
            queues.remove(queue)
        except ValueError:
            pass  # 如果要取消订阅的队列不存在直接忽略
        # 该会话已经没有订阅者时清理这个会话条目
        if not queues:
            self._queues.pop(session_id, None)

    async def publish(self, event: AgentEvent) -> None:
        """发布事件到该会话队列"""
        for queue in list(self._queues.get(event.session_id, [])):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # 如果队列满了，直接丢弃此事件

    def has_subscribers(self, session_id: str) -> bool:
        """检查当前会话是否还有消费者"""
        return bool(self._queues.get(session_id))

# 总线单例
event_bus = EventBus()