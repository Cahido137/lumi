"""事件总线单元测试"""

from app.core.event_bus import EventBus
from app.core.event_response import TokenResponse
from app.core.events import AgentEvent
from app.schemas.enums import EventType


def make_event(session_id: str = "s1") -> AgentEvent:
    """构造测试用事件"""
    return AgentEvent(eventType=EventType.TOKEN, sessionId=session_id, data=TokenResponse(token="hi"))


async def test_subscribe_publish_receive():
    """订阅后会话内事件收发测试"""
    bus = EventBus()
    queue = bus.subscribe("s1")
    await bus.publish(make_event("s1"))  # 发送
    event = queue.get_nowait()  # 接收
    assert event.session_id == "s1"
    assert event.event_type.value == "token"


async def test_publish_isolation_between_sessions():
    """事件队列会话间隔离测试"""
    bus = EventBus()
    q1 = bus.subscribe("s1")
    q2 = bus.subscribe("s2")
    await bus.publish(make_event("s1"))
    assert q1.qsize() == 1
    assert q2.qsize() == 0


async def test_multiple_subscribers_each_get_copy():
    """同一会话多个订阅者是否都能收到资源测试"""
    bus = EventBus()
    q1 = bus.subscribe("s1")
    q2 = bus.subscribe("s1")
    await bus.publish(make_event("s1"))
    assert q1.qsize() == 1
    assert q2.qsize() == 1


async def test_unsubscribe_only_removes_own_queue():
    """测试退订是否只移除自己的队列而不影响其他同会话订阅者"""
    bus = EventBus()
    q1 = bus.subscribe("s1")
    q2 = bus.subscribe("s1")
    bus.unsubscribe("s1", q1)
    assert bus.has_subscribers("s1") is True
    await bus.publish(make_event("s1"))
    assert q2.qsize() == 1


async def test_unsubscribe_last_cleans_session():
    """测试一个会话全部订阅者都退订后是否清空队列"""
    bus = EventBus()
    q1 = bus.subscribe("s1")
    q2 = bus.subscribe("s1")
    bus.unsubscribe("s1", q1)
    assert bus.has_subscribers("s1") is True
    bus.unsubscribe("s1", q2)
    assert bus.has_subscribers("s1") is False
    await bus.publish(make_event("s1"))  # 检查在没有订阅者时发布事件是否报错


async def test_publish_drops_when_queue_full(monkeypatch):
    """测试队列满时是否丢弃事件防止阻塞"""
    monkeypatch.setattr("app.core.event_bus.MAX_QUEUE_SIZE", 2)
    bus = EventBus()
    queue = bus.subscribe("s1")
    queue.put_nowait(make_event())
    queue.put_nowait(make_event())
    assert queue.qsize() == 2
    await bus.publish(make_event())
    assert queue.qsize() == 2
