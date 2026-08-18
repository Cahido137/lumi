"""会话运行器"""

from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage

from app.core.event_bus import event_bus
from app.core.events import AgentEvent, EventType
from app.core.event_response import (
    ToolStartedResponse, 
    ToolFinishedResponse, 
    AgentStartResponse, 
    AgentFinishedResponse, 
    ErrorResponse, 
    PlanUpdatedResponse
)
from app.core.graph.builder import SYSTEM_PROMPT, build_agent_graph
from app.crud import messages as messages_crud
from app.crud import todos as todos_crud
from app.db.models import Message
from app.db.session import SessionLocal


# 图单例
_agent_graph = build_agent_graph()


def _to_langchain_messages(rows) -> list[BaseMessage]:
    """将数据库中的消息行转化为langchain风格消息列表"""
    messages: list[BaseMessage] = []
    for row in rows:
        if row.role == "user":
            messages.append(HumanMessage(content=row.content))
        elif row.role == "assistant":
            messages.append(AIMessage(content=row.content))
    return messages

async def _publish_plan(session_id: str, todos: list[dict]) -> None:
    """发布计划更新事件"""
    await event_bus.publish(AgentEvent(
        eventType=EventType.PLAN_UPDATED,
        sessionId=session_id,
        data=PlanUpdatedResponse(todos=todos)
    ))


async def run_agent_session(session_id: str, content: str) -> Message:
    """运行一轮 Agent 对话"""
    # 发布开始事件
    await event_bus.publish(AgentEvent(
        eventType=EventType.AGENT_STARTED,
        sessionId=session_id,
        data=AgentStartResponse()
    ))

    try:
        async with SessionLocal() as db:
            # 拼接消息
            history = _to_langchain_messages(
                await messages_crud.list_message_asc(db, session_id)
            )
            messages = [SystemMessage(content=SYSTEM_PROMPT)] + history + [HumanMessage(content=content)]

            # 用户消息落库
            await messages_crud.add_message(db, session_id, "user", content)
            await db.commit()

            # 计划队列
            plan_queue: list[dict] = []

            # 以流式方式运行图
            final_reply = ""
            async for chunk in _agent_graph.astream(
                {"messages": messages},
                stream_mode="updates"
            ):
                # 找到计划器节点输出
                if "planner_node" in chunk:
                    plan_queue = sorted(chunk["planner_node"]["todos"], key=lambda t: t["position"])  # 按顺序组织计划队列
                    await todos_crud.replace_todos(db, session_id, plan_queue)
                    await db.commit()
                    await _publish_plan(session_id, plan_queue)
                
                # 找到模型节点输出
                if "model_node" in chunk:
                    msg = chunk["model_node"]["messages"][-1]  # 拿到最近一条消息
                    if msg.tool_calls:
                        for tool in msg.tool_calls:
                            # 修改状态
                            current = next(
                                (t for t in plan_queue if t["status"] == "pending"), None
                            )
                            if current is not None:
                                # 更新计划状态
                                current["status"] = "in_progress"
                                await todos_crud.update_todo_status(db, current["id"], "in_progress")
                                await db.commit()
                                await _publish_plan(session_id, plan_queue)
                            # 发布工具开始执行事件
                            await event_bus.publish(AgentEvent(
                                eventType=EventType.TOOL_STARTED,
                                sessionId=session_id,
                                data=ToolStartedResponse(tool=tool["name"], tool_input=tool["args"])
                            ))
                    else:
                        final_reply = msg.content

                # 找到工具节点输出
                if "tool_node" in chunk:
                    for tm in chunk["tool_node"]["messages"]:
                        # 修改状态
                        current = next(
                            (t for t in plan_queue if t["status"] == "pending"), None
                        )
                        if current is not None:
                            # 更新计划状态
                            current["status"] = "in_progress"
                            await todos_crud.update_todo_status(db, current["id"], "in_progress")
                            await db.commit()
                            await _publish_plan(session_id, plan_queue)
                        # 发布工具结束执行事件
                        await event_bus.publish(AgentEvent(
                            eventType=EventType.TOOL_FINISHED,
                            sessionId=session_id,
                            data=ToolFinishedResponse(tool=tm.name, tool_output=tm.content)
                        ))

                # AI 消息落库
            ai_message = await messages_crud.add_message(db, session_id, "assistant", final_reply)
            await db.commit()

            # 发布结束事件
            await event_bus.publish(AgentEvent(
                eventType=EventType.AGENT_FINISHED,
                sessionId=session_id,
                data=AgentFinishedResponse(reply=final_reply)
            ))
            return ai_message

    except Exception as e:
        # 发布错误事件
        await event_bus.publish(AgentEvent(
            eventType=EventType.ERROR,
            sessionId=session_id,
            data=ErrorResponse(message=str(e))
        ))
        raise