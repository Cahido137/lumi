"""会话运行器辅助函数"""

from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.event_bus import event_bus
from app.core.event_response import PlanUpdatedResponse
from app.core.events import AgentEvent
from app.core.plan_queue import PlanQueue
from app.core.session_runner.context import RunContext
from app.crud import messages as messages_crud
from app.crud import sessions as sessions_crud
from app.crud import todos as todos_crud
from app.db.models import Message
from app.schemas.enums import EventType, MessageRole

# 数据库消息角色映射
_ROLE_MESSAGE: dict[str, type[BaseMessage]] = {
    MessageRole.USER.value: HumanMessage,
    MessageRole.ASSISTANT.value: AIMessage,
    MessageRole.SYSTEM.value: SystemMessage,
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
            for later in messages[i + 1 :]:
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
                sanitized.append(AIMessage(content=msg.content, id=msg.id, usage_metadata=msg.usage_metadata))
        # 其余消息直接合并
        else:
            sanitized.append(msg)

    # 丢弃没有AIMessage中工具调用请求的ToolMessage
    result: list[BaseMessage] = []
    valid_ids: set[str] = set()
    for msg in sanitized:
        if isinstance(msg, AIMessage):
            valid_ids = {tc["id"] for tc in msg.tool_calls if tc["id"] is not None}  # 合法工具调用id
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
            messages.append(
                ToolMessage(content=row.content, tool_call_id=row.tool_call_id or "", name=row.tool_name, id=row.id)
            )
            continue
        factory = _ROLE_MESSAGE.get(row.role)
        # 不支持的消息类型
        if factory is None:
            raise ValueError("不支持的消息类型")
        if factory is AIMessage:
            kwargs: dict[str, Any] = {"content": row.content, "id": row.id, "usage_metadata": row.usage}
            if row.tool_calls:
                kwargs["tool_calls"] = row.tool_calls
            messages.append(AIMessage(**kwargs))
        else:
            messages.append(factory(content=row.content, id=row.id))
    return _sanitize_dangling_tool_calls(messages)


async def rebuild_history(db: AsyncSession, session_id: str, exclude_id: str | None = None) -> list[BaseMessage]:
    """从数据库重建本次图运行的历史消息"""
    summary_text, until_id = await sessions_crud.get_context_summary(db, session_id)
    if summary_text and until_id:
        boundary = await messages_crud.get_message_by_id(db, until_id)  # 获取压缩边界消息
        if boundary is not None and boundary.session_id == session_id:
            rows = await messages_crud.list_messages_after(db, session_id, until_id)
            if exclude_id is not None:
                rows = [row for row in rows if row.id != exclude_id]
            return [HumanMessage(content=summary_text, id=until_id)] + to_langchain_messages(rows)
        await sessions_crud.set_context_summary(db, session_id, None, None)
        await db.commit()
    rows = await messages_crud.list_message_asc(db, session_id)
    if exclude_id is not None:
        rows = [row for row in rows if row.id != exclude_id]
    return to_langchain_messages(rows)


def build_config(session_id: str) -> RunContext:
    """生成运行config与thread_id"""
    thread_id = f"{session_id}:{uuid4()}"  # 格式为会话ID+一个uuid
    return RunContext(config={"configurable": {"thread_id": thread_id}}, thread_id=thread_id)


async def load_plan_queue(db: AsyncSession, session_id: str) -> PlanQueue:
    """从todos表重载计划队列"""
    rows = await todos_crud.list_todos(db, session_id)
    return PlanQueue.from_rows(rows)


async def publish_plan(session_id: str, plan_queue: PlanQueue) -> None:
    """发布计划更新事件"""
    await event_bus.publish(
        AgentEvent(
            event_type=EventType.PLAN_UPDATED,
            session_id=session_id,
            data=PlanUpdatedResponse(todos=plan_queue.to_list()),
        )
    )
