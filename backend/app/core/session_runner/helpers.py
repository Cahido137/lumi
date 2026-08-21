"""会话运行器辅助函数"""

from uuid import uuid4

from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.event_bus import event_bus
from app.core.events import AgentEvent
from app.core.event_response import PlanUpdatedResponse
from app.core.plan_queue import PlanQueue
from app.core.session_runner.context import RunContext
from app.crud import todos as todos_crud
from app.schemas.enums import EventType, MessageRole


def to_langchain_messages(rows) -> list[BaseMessage]:
    """将数据库中的消息行转化为langchain风格消息列表"""
    messages: list[BaseMessage] = []
    for row in rows:
        if row.role == MessageRole.USER:
            messages.append(HumanMessage(content=row.content))
        elif row.role == MessageRole.ASSISTANT:
            messages.append(AIMessage(content=row.content))
    return messages

def build_config(session_id: str) -> RunContext:
    """生成运行config与thread_id"""
    thread_id = f"{session_id}:{uuid4()}"  # 格式为会话ID+一个uuid
    return RunContext(
        config={"configurable": {"thread_id": thread_id}},
        thread_id=thread_id
    )

async def load_plan_queue(db: AsyncSession, session_id: str) -> PlanQueue:
    """从todos表重载计划队列"""
    rows = await todos_crud.list_todos(db, session_id)
    return PlanQueue.from_rows(rows)

async def publish_plan(session_id: str, plan_queue: PlanQueue) -> None:
    """发布计划更新事件"""
    await event_bus.publish(AgentEvent(
        eventType=EventType.PLAN_UPDATED,
        sessionId=session_id,
        data=PlanUpdatedResponse(todos=plan_queue.to_list())
    ))