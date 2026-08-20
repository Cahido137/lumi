"""会话运行器"""

from uuid import uuid4

from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage
from langgraph.types import Command

from app.core.checkpoint import get_checkpointer
from app.core.event_bus import event_bus
from app.core.events import AgentEvent, EventType
from app.core.event_response import (
    ToolStartedResponse, 
    ToolFinishedResponse, 
    AgentStartResponse, 
    AgentFinishedResponse, 
    ErrorResponse, 
    PlanUpdatedResponse,
    ApprovalRequiredResponse,
    ApprovalResultResponse,
    TokenResponse
)
from app.core.tools.todo_tool import TODO_MARKER_TOOL
from app.core.graph.builder import SYSTEM_PROMPT, build_agent_graph
from app.crud import messages as messages_crud
from app.crud import todos as todos_crud
from app.crud import approvals as approvals_crud
from app.crud import tool_executions as tool_executions_crud
from app.db.models import Message
from app.db.session import SessionLocal


# 图单例
_agent_graph = None

def _get_agent_graph():
    """获取图单例"""
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = build_agent_graph(get_checkpointer())
    return _agent_graph


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

def _build_config(session_id: str) -> tuple[dict, str]:
    """生成运行config与thread_id"""
    thread_id = f"{session_id}:{uuid4()}"  # 格式为会话ID+一个uuid
    return {"configurable": {"thread_id": thread_id}}, thread_id

async def _load_plan_queue(db, session_id: str) -> list[dict]:
    """从todos表重载计划队列"""
    rows = await todos_crud.list_todos(db, session_id)
    return [
        {"id": todo.id, "title": todo.title, "status": todo.status, "position": todo.position}
        for todo in rows
    ]


async def _process_stream(db, session_id: str, plan_queue: list[dict], graph_input, config) -> tuple[str, dict | None]:
    """图运行与事件处理流"""
    # 以流式方式运行图
    final_reply = ""
    interrupt_info: dict | None = None
    async for chunk in _get_agent_graph().astream(
        graph_input,
        config=config,
        stream_mode=["updates", "messages"]
    ):
        mode, payload = chunk  # 分离流式块中的信息

        if mode == "messages":
            msg_chunk, meta_data = payload
            node = meta_data.get("langgraph_node")
            content = msg_chunk.content
            # 如果流式块来自大模型节点并且非空
            if node == "model_node" and isinstance(content, str) and content:
                # 发布流式输出事件
                await event_bus.publish(AgentEvent(
                    eventType=EventType.TOKEN,
                    sessionId=session_id,
                    data=TokenResponse(token=content)
                ))
            continue

        # 检查是否有中断
        if "__interrupt__" in payload:
            interrupt_info = payload["__interrupt__"][0].value  # 提取中断信息
            break

        # 找到计划器节点输出
        if "planner_node" in payload:
            plan_queue = sorted(payload["planner_node"]["todos"], key=lambda t: t["position"])  # 按顺序组织计划队列
            await todos_crud.replace_todos(db, session_id, plan_queue)
            await db.commit()
            await _publish_plan(session_id, plan_queue)
                
        # 找到模型节点输出
        if "model_node" in payload:
            msg = payload["model_node"]["messages"][-1]  # 拿到最近一条消息
            if msg.tool_calls:
                # 提取出除开todo标记工具的其他工具
                real_calls = [tool for tool in msg.tool_calls if tool["name"] != TODO_MARKER_TOOL]
                # 确保每次只有至多一个todo处于执行中
                # 从队列中取出所有todo计划，只有在是除todo标记工具执行并且没有正在运行的todo才将下一条todo标记为执行中
                if real_calls and not any(t["status"] == "in_progress" for t in plan_queue):
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
                for tool in msg.tool_calls:
                    # 如果调用的是todo标记工具，直接跳过，不进行工具调用事件发布
                    if tool["name"] == TODO_MARKER_TOOL:
                        continue
                    await event_bus.publish(AgentEvent(
                        eventType=EventType.TOOL_STARTED,
                        sessionId=session_id,
                        data=ToolStartedResponse(tool=tool["name"], tool_input=tool["args"])
                    ))
            else:
                final_reply = msg.content

        # 找到工具节点输出
        if "tool_node" in payload:
            for tm in payload["tool_node"]["messages"]:
                # 如果是todo标记工具，同步计划状态后直接跳过
                if tm.name == TODO_MARKER_TOOL:
                    plan_queue = await _load_plan_queue(db, session_id)  # 重新从数据库读入计划列表刷新
                    await _publish_plan(session_id, plan_queue)
                    continue

                # 先提取出消息块，并确保是字符串
                content = tm.content if isinstance(tm.content, str) else str(tm.content)
                rejected = content.startswith("[REJECTED]")  # 判断审批是否被拒绝
                skipped = content.startswith("[SKIPPED]")  # 判断此工具消息是否因为触发了多重中断而被跳过

                # 如果刚批准，落库执行结果
                # 没有被跳过才执行落库
                if not skipped:
                    pending = await tool_executions_crud.get_pending_execution(db, session_id)
                    if (pending is not None) and (pending.tool_name == tm.name):
                        status = "rejected" if rejected else "success"
                        await tool_executions_crud.finish_execution(db, pending.id, status, tm.content)
                        await db.commit()

                # 修改状态
                current = next(
                    (t for t in plan_queue if t["status"] == "in_progress"), None
                )
                if current is not None:
                    if rejected:
                        new_status = "failed"
                    elif skipped:
                        new_status = "pending"
                    else:
                        new_status = None
                    # 更新计划状态
                    if new_status is not None:
                        current["status"] = new_status
                        await todos_crud.update_todo_status(db, current["id"], new_status)
                        await db.commit()
                        await _publish_plan(session_id, plan_queue)

                # 发布工具结束执行事件
                await event_bus.publish(AgentEvent(
                    eventType=EventType.TOOL_FINISHED,
                    sessionId=session_id,
                    data=ToolFinishedResponse(tool=tm.name, tool_output=tm.content)
                ))

    # 如果图已经运行结束了，关闭所有还在执行的步骤
    if interrupt_info is None:
        current = next(
            (t for t in plan_queue if t["status"] == "in_progress"), None
        )
        if current is not None:
            current["status"] = "done"
            await todos_crud.update_todo_status(db, current["id"], "done")
            await db.commit()
            await _publish_plan(session_id, plan_queue)
    return final_reply, interrupt_info


async def run_agent_session(session_id: str, content: str) -> Message | None:
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

            config, thread_id = _build_config(session_id)  # 创建配置
            grants = await approvals_crud.get_session_grants(db, session_id)  # 获取当前会话工具授权
            final_reply, interrupted_info = await _process_stream(
                db, session_id, [], {"messages": messages, "grants": grants}, config
            )

            # 如果有中断信息则创建审批相关信息并落库，并且发布审批事件到总线
            if interrupted_info is not None:
                execution = await tool_executions_crud.create_pending_execution(
                    db, session_id, interrupted_info["tool"], interrupted_info["tool_input"]
                )
                approval = await approvals_crud.create_approval(
                    db, session_id, thread_id, execution.id
                )
                await db.commit()
                await event_bus.publish(AgentEvent(
                    eventType=EventType.APPROVAL_REQUIRED,
                    sessionId=session_id,
                    data=ApprovalRequiredResponse(
                        approval_id=approval.id,
                        tool=interrupted_info["tool"],
                        tool_input=interrupted_info["tool_input"]
                    )
                ))
                return None

            # 最后回答
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


async def resume_agent_session(approval_id: str, decision: str, scope: str = "one_time") -> str | None:
    """审批完成，恢复图的执行"""
    async with SessionLocal() as db:
        approval = await approvals_crud.get_approval_by_id(db, approval_id)  # 拿到审批单
        if approval is None:
            raise ValueError("审批单不存在")
        if approval.status != "pending":
            raise ValueError("审批单已处理")

        # 更新审批单
        await approvals_crud.update_approval(db, approval_id, decision, scope)
        await db.commit()

        # 发布审批结束事件到总线
        await event_bus.publish(AgentEvent(
            eventType=EventType.APPROVAL_RESULT,
            sessionId=approval.session_id,
            data=ApprovalResultResponse(
                approval_id=approval.id,
                status=decision
            )
        ))

        # 恢复图的执行
        config = {"configurable": {"thread_id": approval.thread_id}}
        plan_queue = await _load_plan_queue(db, approval.session_id)
        grants = await approvals_crud.get_session_grants(db, approval.session_id)
        final_reply, interrupt_info = await _process_stream(
            db, approval.session_id, plan_queue, Command(resume=decision, update={"grants": grants}), config
        )

        # 再次检查是否还有中断
        if interrupt_info is not None:
            execution = await tool_executions_crud.create_pending_execution(
                db, approval.session_id, interrupt_info["tool"], interrupt_info["tool_input"]
            )
            new_approval = await approvals_crud.create_approval(
                db, approval.session_id, approval.thread_id, execution.id
            )
            await db.commit()
            await event_bus.publish(AgentEvent(
                eventType=EventType.APPROVAL_REQUIRED,
                sessionId=approval.session_id,
                data=ApprovalRequiredResponse(
                    approval_id=new_approval.id,
                    tool=interrupt_info["tool"],
                    tool_input=interrupt_info["tool_input"]
                )
            ))
            return None
        ai_message = await messages_crud.add_message(
            db, approval.session_id, "assistant", final_reply
        )
        await db.commit()

    await event_bus.publish(AgentEvent(
        eventType=EventType.AGENT_FINISHED,
        sessionId=approval.session_id,
        data=AgentFinishedResponse(reply=final_reply)
    ))
    return final_reply