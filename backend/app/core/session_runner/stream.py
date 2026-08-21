"""图流处理"""

from app.core.checkpoint import get_checkpointer
from app.core.event_bus import event_bus
from app.core.events import AgentEvent
from app.core.event_response import (
    TokenResponse,
    ToolStartedResponse,
    ToolFinishedResponse
)
from app.core.graph.builder import REJECTED_PREFIX, SKIPPED_PREFIX, build_agent_graph
from app.core.graph.schemas import ApprovalInterrupt
from app.core.plan_queue import PlanQueue
from app.core.session_runner.context import StreamResult
from app.core.session_runner.helpers import load_plan_queue, publish_plan
from app.core.tools.todo_tool import TODO_MARKER_TOOL
from app.crud import todos as todos_crud
from app.crud import tool_executions as tool_executions_crud
from app.schemas.enums import EventType, TodoStatus, ExecutionStatus
from app.schemas.todos import TodoItem


# 图单例
_agent_graph = None

def get_agent_graph():
    """获取图单例"""
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = build_agent_graph(get_checkpointer())
    return _agent_graph

async def process_stream(db, session_id: str, plan_queue: PlanQueue, graph_input, config) -> StreamResult:
    """图运行与事件处理流"""
    # 以流式方式运行图
    final_reply = ""
    interrupt_info: ApprovalInterrupt | None = None
    async for chunk in get_agent_graph().astream(
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
            interrupt_info = ApprovalInterrupt.model_validate(payload["__interrupt__"][0].value)  # 提取中断信息
            break

        # 找到计划器节点输出
        if "planner_node" in payload:
            items = [
                TodoItem.model_validate(t)
                for t in payload["planner_node"]["todos"]
            ]
            plan_queue.items = sorted(items, key=lambda t: t.position)
            await todos_crud.replace_todos(db, session_id, plan_queue.to_list())
            await db.commit()
            await publish_plan(session_id, plan_queue)
                
        # 找到模型节点输出
        if "model_node" in payload:
            msg = payload["model_node"]["messages"][-1]  # 拿到最近一条消息
            if msg.tool_calls:
                # 提取出除开todo标记工具的其他工具
                real_calls = [tool for tool in msg.tool_calls if tool["name"] != TODO_MARKER_TOOL]
                # 确保每次只有至多一个todo处于执行中
                # 从队列中取出所有todo计划，只有在是除todo标记工具执行并且没有正在运行的todo才将下一条todo标记为执行中
                if real_calls and not plan_queue.has_in_progress():
                    # 开始下一个计划
                    current = plan_queue.start_next()
                    if current is not None:
                        # 更新计划状态
                        await todos_crud.update_todo_status(db, current.id, TodoStatus.IN_PROGRESS)
                        await db.commit()
                        await publish_plan(session_id, plan_queue)
                # 发布工具开始执行事件
                for tool in msg.tool_calls:
                    # 如果调用的是todo标记工具，直接跳过，不进行工具调用事件发布
                    if tool["name"] == TODO_MARKER_TOOL:
                        continue
                    await event_bus.publish(AgentEvent(
                        eventType=EventType.TOOL_STARTED,
                        sessionId=session_id,
                        data=ToolStartedResponse(tool=tool["name"], tool_input=tool["args"] or {})
                    ))
            else:
                final_reply = msg.content

        # 找到工具节点输出
        if "tool_node" in payload:
            for tm in payload["tool_node"]["messages"]:
                # 如果是todo标记工具，同步计划状态后直接跳过
                if tm.name == TODO_MARKER_TOOL:
                    plan_queue = await load_plan_queue(db, session_id)  # 重新从数据库读入计划列表刷新
                    await publish_plan(session_id, plan_queue)
                    continue

                # 先提取出消息块，并确保是字符串
                content = tm.content if isinstance(tm.content, str) else str(tm.content)
                rejected = content.startswith(REJECTED_PREFIX)  # 判断审批是否被拒绝
                skipped = content.startswith(SKIPPED_PREFIX)  # 判断此工具消息是否因为触发了多重中断而被跳过

                # 如果刚批准，落库执行结果
                # 没有被跳过才执行落库
                if not skipped:
                    pending = await tool_executions_crud.get_pending_execution(db, session_id)
                    if (pending is not None) and (pending.tool_name == tm.name):
                        status = ExecutionStatus.REJECTED if rejected else ExecutionStatus.SUCCESS
                        await tool_executions_crud.finish_execution(db, pending.id, status, tm.content)
                        await db.commit()

                # 修改状态
                current = plan_queue.get_in_progress()
                if current is not None:
                    if rejected:
                        plan_queue.resolve_current(TodoStatus.FAILED)
                        await todos_crud.update_todo_status(db, current.id, TodoStatus.FAILED)
                        await db.commit()
                        await publish_plan(session_id, plan_queue)
                    elif skipped:
                        plan_queue.resolve_current(TodoStatus.PENDING)
                        await todos_crud.update_todo_status(db, current.id, TodoStatus.PENDING)
                        await db.commit()
                        await publish_plan(session_id, plan_queue)
                    
                # 发布工具结束执行事件
                await event_bus.publish(AgentEvent(
                    eventType=EventType.TOOL_FINISHED,
                    sessionId=session_id,
                    data=ToolFinishedResponse(tool=tm.name, tool_output=tm.content)
                ))

    # 如果图已经运行结束了，关闭所有还在执行的步骤
    if interrupt_info is None:
        current = plan_queue.get_in_progress()
        if current is not None:
            plan_queue.resolve_current(TodoStatus.DONE)
            await todos_crud.update_todo_status(db, current.id, TodoStatus.DONE)
            await db.commit()
            await publish_plan(session_id, plan_queue)
    return StreamResult(final_reply=final_reply, interrupt=interrupt_info)