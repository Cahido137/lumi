"""事件总线"""

import asyncio

from app.core.events import AgentEvent


MAX_QUEUE_SIZE = 1000

class EventBus:
    """事件总线类"""
    def __init__(self):
        # 会话事件队列
        self._queues: dict[str, asyncio.Queue] = {}

    def subscribe(self, session_id: str) -> asyncio.Queue:
        """消费者订阅会话事件流"""
        # 检查是否已订阅保持幂等
        if session_id not in self._queues:
            self._queues[session_id] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        return self._queues[session_id]

    def unsubscribe(self, session_id: str) -> None:
        """消费者取消订阅会话事件流"""
        self._queues.pop(session_id, None)

    async def publish(self, event: AgentEvent) -> None:
        """发布事件到该会话队列"""
        queue = self._queues.get(event.session_id)
        if queue is not None:
            await queue.put(event)

    def has_subscribers(self, session_id: str) -> bool:
        """检查当前会话是否还有消费者"""
        return session_id in self._queues

# 总线单例
event_bus = EventBus()