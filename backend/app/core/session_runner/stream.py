"""图流处理"""

import asyncio
from functools import partial

from app.core.checkpoint import get_checkpointer
from app.core.event_bus import event_bus
from app.core.events import AgentEvent
from app.core.event_response import (
    TokenResponse,
    ToolStartedResponse,
    ToolFinishedResponse
)
from app.core.graph.builder import build_agent_graph
from app.core.graph.schemas import ApprovalInterrupt
from app.core.plan_queue import PlanQueue
from app.core.session_runner.context import StreamResult
from app.core.session_runner.helpers import load_plan_queue, publish_plan
from app.core.session_runner.state import (
    RunCancelledError,
    _CANCELLED,
    register_active_task,
    unregister_active_task
)
from app.core.tools.todo_tool import TODO_MARKER_TOOLS
from app.crud import todos as todos_crud
from app.crud import tool_executions as tool_executions_crud
from app.crud import approvals as approvals_crud
from app.crud import sessions as sessions_crud
from app.schemas.enums import EventType, TodoStatus, ExecutionStatus, ApprovalStatus
from app.schemas.todos import TodoItem


# 图单例
_agent_graph = None

def get_agent_graph():
    """获取图单例"""
    global _agent_graph
    if _agent_graph is None:
        _agent_graph = build_agent_graph(get_checkpointer())
    return _agent_graph

async def _produce_stream(graph_input, config, chunks: asyncio.Queue) -> None:
    """图流生产者"""
    async for chunk in get_agent_graph().astream(
        graph_input,
        config=config,
        stream_mode=["updates", "messages"]
    ):
        await chunks.put(chunk)  # 生产者将 chunk 存入队列
    await chunks.put(None)

def _on_producer_done(chunks: asyncio.Queue, task: asyncio.Task) -> None:
    """生产者结束回调"""
    if task.cancelled():
        # 如果任务已经被取消则取消哨兵
        chunks.put_nowait(_CANCELLED)
        return
    exc = task.exception()  # 捕获取消任务的异常
    if exc is not None:
        chunks.put_nowait(exc)

def _resolve_execution_status(rejected: bool, msg_status: str | None) -> ExecutionStatus:
    """由审批单状态和工具消息状态决定执行记录状态"""
    if rejected:
        return ExecutionStatus.REJECTED
    if msg_status == "error":
        return ExecutionStatus.ERROR
    return ExecutionStatus.SUCCESS

async def process_stream(db, session_id: str, plan_queue: PlanQueue, graph_input, config, cancel_event: asyncio.Event | None = None) -> StreamResult:
    """图运行与事件处理流"""
    # 以流式方式运行图
    final_reply = ""
    interrupt_info: ApprovalInterrupt | None = None
    streamed_parts: list[str] = []  # 保存打断前已经流式输出出来的文本片段

    # chunk 队列
    chunks: asyncio.Queue = asyncio.Queue()

    producer = asyncio.create_task(_produce_stream(graph_input, config, chunks))  # 创建生产者任务
    register_active_task(session_id, producer)  # 将生产者注册进会话
    producer.add_done_callback(partial(_on_producer_done, chunks))  # 为生产者绑定回调函数

    try:
        executed_any = False  # 记录本轮是否执行过任何工具
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RunCancelledError(streamed_text="".join(streamed_parts))
            chunk = await chunks.get()
            # 如果生产者被取消
            if chunk is _CANCELLED:
                raise RunCancelledError(streamed_text="".join(streamed_parts))
            # 如果图是正常结束
            if chunk is None:
                break
            # 如果图流出现异常
            if isinstance(chunk, BaseException):
                raise chunk  # 继续向上抛出异常
            mode, payload = chunk  # 分离流式块中的信息

            if mode == "messages":
                msg_chunk, meta_data = payload
                node = meta_data.get("langgraph_node")
                content = msg_chunk.content
                # 如果流式块来自大模型节点并且非空
                if node == "model_node" and isinstance(content, str) and content:
                    streamed_parts.append(content)  # 记录已经生成的流式文本
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
                planner_output = payload.get("planner_node") or {}
                # 如果规划期返回空，则跳过替换
                if "todos" not in planner_output:
                    continue
                items = [
                    TodoItem.model_validate(t)
                    for t in payload["planner_node"]["todos"]
                ]
                plan_queue.items = sorted(items, key=lambda t: t.position)
                await todos_crud.replace_todos(db, session_id, plan_queue.to_list())
                await db.commit()
                await sessions_crud.set_has_pending_task(db, session_id, False)  # 作废旧计划，清除标记
                await db.commit()
                await publish_plan(session_id, plan_queue)
                    
            # 找到模型节点输出
            if "model_node" in payload:
                msg = payload["model_node"]["messages"][-1]  # 拿到最近一条消息
                if msg.tool_calls:
                    for tool in msg.tool_calls:
                        # todo标记工具不发布
                        if tool["name"] in TODO_MARKER_TOOLS:
                            continue
                        # 发布工具开始执行事件
                        await event_bus.publish(AgentEvent(
                            eventType=EventType.TOOL_STARTED,
                            sessionId=session_id,
                            data=ToolStartedResponse(
                                tool=tool["name"],
                                tool_input=tool["args"] or {}
                            )
                        ))
                else:
                    final_reply = msg.content

            # 找到工具节点输出
            if "exec_node" in payload:
                executed_any = True
                for tm in payload["exec_node"]["messages"]:
                    # 如果是todo标记工具，同步计划状态后直接跳过
                    if tm.name in TODO_MARKER_TOOLS:
                        plan_queue = await load_plan_queue(db, session_id)  # 重新从数据库读入计划列表刷新
                        await publish_plan(session_id, plan_queue)
                        continue

                    # 先提取出消息块，并确保是字符串
                    content = tm.content if isinstance(tm.content, str) else str(tm.content)

                    pending = await tool_executions_crud.get_pending_execution_by_call_id(db, session_id, tm.tool_call_id)
                    if pending is None:
                        pending = await tool_executions_crud.get_pending_execution(db, session_id)
                    matched = False
                    if pending is not None:
                        if pending.tool_call_id is not None:
                            matched = pending.tool_call_id == tm.tool_call_id
                        else:
                            matched = pending.tool_name == tm.name

                    rejected = False
                    if matched:
                        approval = await approvals_crud.get_approval_by_execution_id(db, pending.id)
                        rejected = approval is not None and approval.status == ApprovalStatus.REJECTED.value
                        status = _resolve_execution_status(rejected, getattr(tm, "status", None))
                        await tool_executions_crud.finish_execution(db, pending.id, status, tm.content)
                        await db.commit()

                    # 审批被拒时把当前执行中的计划步骤回退为失败
                    if rejected:
                        in_progress = plan_queue.get_in_progress_list()
                        if len(in_progress) == 1:
                            target = in_progress[0]
                            await todos_crud.update_todo_status(db, target.id, TodoStatus.FAILED)
                            await db.commit()
                            await publish_plan(session_id, plan_queue)
                        
                    # 发布工具结束执行事件
                    await event_bus.publish(AgentEvent(
                        eventType=EventType.TOOL_FINISHED,
                        sessionId=session_id,
                        data=ToolFinishedResponse(tool=tm.name, tool_output=tm.content)
                    ))
    finally:
        unregister_active_task(session_id)  # 注销任务
        # 如果生产者还没结束则强制结束
        if not producer.done():
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    # 如果图是正常结束的，将所有正在执行的任务结束
    if interrupt_info is None and executed_any:
        rows = await todos_crud.list_todos(db, session_id)  # 列出所有任务
        finished_any = False
        for row in rows:
            if row.status == TodoStatus.IN_PROGRESS.value:
                await todos_crud.update_todo_status(db, row.id, TodoStatus.DONE)
                finished_any = True
        if finished_any:
            await db.commit()
            plan_queue = await load_plan_queue(db, session_id)  # 重新加载列表以发布最新计划
            await publish_plan(session_id, plan_queue)
        # 检查是否有还未完成的任务
        remaining = [row for row in await todos_crud.list_todos(db, session_id) if row.status != TodoStatus.DONE.value]
        if not remaining:
            await sessions_crud.set_has_pending_task(db, session_id, False)
            await db.commit()
    return StreamResult(final_reply=final_reply, interrupt=interrupt_info)