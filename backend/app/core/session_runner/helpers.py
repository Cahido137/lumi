"""会话运行器辅助函数"""

from uuid import uuid4
from collections.abc import Sequence

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage, BaseMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.event_bus import event_bus
from app.core.events import AgentEvent
from app.core.event_response import PlanUpdatedResponse
from app.core.plan_queue import PlanQueue
from app.core.session_runner.context import RunContext
from app.crud import todos as todos_crud
from app.schemas.enums import EventType, MessageRole
from app.db.models import Message


# 数据库消息角色映射
_ROLE_MESSAGE: dict[str, type[BaseMessage]] = {
    MessageRole.USER.value: HumanMessage,
    MessageRole.ASSISTANT.value: AIMessage,
    MessageRole.SYSTEM.value: SystemMessage
}

def _sanitize_dangling_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """清除悬空工具调用, 将未执行的工具调用清除"""
    # 检查每个工具调用请求是否都有相对应的ToolMessage响应
    sanitized: list[BaseMessage] = []
    for i, msg in enumerate(messages):
        # 检查AIMessage
        if isinstance(msg, AIMessage) and msg.tool_calls:
            declared = {tc["id"] for tc in msg.tool_calls}  # AIMessage声明的需要的工具调用
            answered: set[str] = set()  # 记录ToolMessage工具回复
            # 检查这条AIMessage之后，下一条AIMessage之前是否有回复的ToolMessage
            for later in messages[i + 1:]:
                # 到下一条AIMessage了
                if isinstance(later, AIMessage):
                    break
                if isinstance(later, ToolMessage):
                    answered.add(later.tool_call_id)
            # 如果AIMessage提出的工具调用都已经完成
            if declared <= answered:
                sanitized.append(msg)
            # 如果没完整地调用完工具就只存储消息文本丢弃tool_call
            else:
                sanitized.append(AIMessage(content=msg.content))
        # 其余消息直接合并
        else:
            sanitized.append(msg)

    # 丢弃没有AIMessage中工具调用请求的ToolMessage
    result: list[BaseMessage] = []
    valid_ids: set[str] = set()
    for msg in sanitized:
        if isinstance(msg, AIMessage):
            valid_ids = {tc["id"] for tc in msg.tool_calls}  # 合法工具调用id
            result.append(msg)
        elif isinstance(msg, ToolMessage):
            # 只有在合法调用id列表中的工具调用才保留
            if msg.tool_call_id in valid_ids:
                result.append(msg)
        else:
            valid_ids = set()
            result.append(msg)
    return result

def to_langchain_messages(rows: Sequence[Message]) -> list[BaseMessage]:
    """将数据库中的消息行转化为langchain风格消息列表"""
    messages: list[BaseMessage] = []
    for row in rows:
        # 单独处理ToolMessage
        if row.role == MessageRole.TOOL.value:
            messages.append(ToolMessage(
                content=row.content,
                tool_call_id=row.tool_call_id or "",
                name=row.tool_name
            ))
            continue
        factory = _ROLE_MESSAGE.get(row.role)
        # 不支持的消息类型
        if factory is None:
            raise ValueError("不支持的消息类型")
        # 如果模型消息是携带工具调用请求的消息也一起拿出转化
        if factory is AIMessage and row.tool_calls:
            messages.append(AIMessage(content=row.content, tool_calls=row.tool_calls))
        else:
            messages.append(factory(content=row.content))
    return _sanitize_dangling_tool_calls(messages)

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