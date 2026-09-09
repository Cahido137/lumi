"""事件总线, 负责会话事件的进程内发布订阅。

Note:
    本总线是进程内单例, 事件不跨进程传播。
"""

import asyncio
import logging

from app.core.events import AgentEvent

MAX_QUEUE_SIZE = 1000

logger = logging.getLogger(__name__)


class EventBus:
    """事件总线类。"""

    def __init__(self):
        """初始化事件总线, 建立会话ID到订阅队列列表的映射。"""
        self._queues: dict[str, list[asyncio.Queue[AgentEvent]]] = {}  # 设计多个队列防止资源竞争

    def subscribe(self, session_id: str) -> asyncio.Queue[AgentEvent]:
        """消费者订阅指定会话的事件流。

        Args:
            session_id: 会话ID。

        Returns:
            asyncio.Queue: 该订阅者独占的事件队列。
        """
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        self._queues.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[AgentEvent]) -> None:
        """消费者取消订阅会话的事件流。

        Args:
            session_id: 会话ID。
            queue: subscribe 函数返回的事件队列。
        """
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
        """发布事件到该会话队列。

        Args:
            event: 待发布的事件。

        Note:
            队列满时会直接丢弃后来的事件。
        """
        for queue in list(self._queues.get(event.session_id, [])):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 如果队列满了，直接丢弃此事件
                logger.warning("事件队列已满丢弃事件: session_id=%s, type=%s", event.session_id, event.event_type.value)

    def has_subscribers(self, session_id: str) -> bool:
        """检查当前会话是否还有消费者订阅。

        Args:
            session_id: 会话ID。

        Returns:
            bool: 是否仍有订阅者。
        """
        return bool(self._queues.get(session_id))


# 总线单例
event_bus = EventBus()
