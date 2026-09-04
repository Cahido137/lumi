"""会话运行器"""

import logging

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.core.event_bus import event_bus
from app.core.events import AgentEvent
from app.core.event_response import (
    AgentFinishedResponse,
    AgentStartResponse,
    ApprovalRequiredResponse,
    ApprovalResultResponse,
    ErrorResponse,
)
from app.core.logging_config import bind_session_id, unbind_session_id
from app.core.prompts import get_system_messages
from app.core.plan_queue import PlanQueue
from app.core.event_response import RunCancelledResponse
from app.core.session_runner.context import StreamResult
from app.core.session_runner.helpers import build_config, load_plan_queue, rebuild_history
from app.core.session_runner.state import (
    get_session_lock,
    get_cancel_event,
    get_cancel_generation,
    register_pending_run,
    unregister_pending_run,
    _active_runs,
    CANCEL_MESSAGE,
    RunCancelledError
)
from app.core.session_runner.stream import process_stream
from app.crud import approvals as approvals_crud
from app.crud import messages as messages_crud
from app.crud import tool_executions as tool_executions_crud
from app.crud import sessions as sessions_crud
from app.crud import todos as todos_crud
from app.db.models import Message
from app.db.session import SessionLocal
from app.schemas.enums import (
    ApprovalScope,
    ApprovalStatus,
    EventType,
    MessageRole,
)
from app.schemas.todos import TodoStatus, TodoItem


logger = logging.getLogger(__name__)

async def _run_agent_session_locked(session_id: str, content: str, *, user_message_id: str | None = None) -> Message | None:
    """执行一轮 Agent 对话。调用方必须已经持有会话锁并通过取消代际校验。"""
    # 清除上一次遗留的取消状态
    cancel_event = get_cancel_event(session_id)
    cancel_event.clear()
    _active_runs.add(session_id)  # 将本会话加入运行队列
    logger.info("开始执行会话轮次: user_message_id=%s", user_message_id or " - ")

    try:
        # 发布开始事件
        await event_bus.publish(AgentEvent(
            eventType=EventType.AGENT_STARTED,
            sessionId=session_id,
            data=AgentStartResponse()
        ))

        # 存在待处理的审批禁止开始新一轮的对话
        async with SessionLocal() as db:
            if await approvals_crud.has_pending_approval(db, session_id):
                raise ValueError("该会话存在未完成的审批, 请先完成审批再开始新对话")

        async with SessionLocal() as db:
            if user_message_id is None:
                user_row = await messages_crud.add_message(db, session_id, MessageRole.USER, content)
                await db.commit()
                user_message_id = user_row.id
            # 重建消息历史
            history = await rebuild_history(db, session_id, exclude_id=user_message_id)
            messages = get_system_messages() + history + [HumanMessage(content=content, id=user_message_id)]

            run_context = build_config(session_id)  # 创建配置
            grants = await approvals_crud.get_session_grants(db, session_id)  # 获取当前会话工具授权
            graph_input = {"messages": messages, "grants": grants.model_dump(), "session_id": session_id}
            # 如果存在因被打断而未完成的任务，则注入先前的完整计划
            if await sessions_crud.get_has_pending_task(db, session_id):
                rows = await todos_crud.list_todos(db, session_id)
                graph_input["todos"] = [
                    TodoItem(id=row.id, title=row.title, status=TodoStatus(row.status), position=row.position)
                    for row in rows
                ]
            stream_result: StreamResult = await process_stream(
                db=db, session_id=session_id, plan_queue=PlanQueue(),
                graph_input=graph_input,
                config=run_context.config,
                cancel_event=cancel_event
            )

            # 如果有中断信息则创建审批相关信息并落库，并且发布审批事件到总线
            if stream_result.interrupt is not None:
                execution = await tool_executions_crud.create_pending_execution(
                    db, session_id, stream_result.interrupt.tool, stream_result.interrupt.tool_input, stream_result.interrupt.tool_call_id
                )
                approval = await approvals_crud.create_approval(
                    db, session_id, run_context.thread_id, execution.id
                )
                await db.commit()
                logger.info("工具待审批: approval_id=%s, tool=%s", approval.id, stream_result.interrupt.tool)
                await event_bus.publish(AgentEvent(
                    eventType=EventType.APPROVAL_REQUIRED,
                    sessionId=session_id,
                    data=ApprovalRequiredResponse(
                        approval_id=approval.id,
                        tool=stream_result.interrupt.tool,
                        tool_input=stream_result.interrupt.tool_input
                    )
                ))
                return None

            # 最后回答
            ai_message = await messages_crud.add_message(
                db, session_id, MessageRole.ASSISTANT, stream_result.final_reply,
                usage=stream_result.final_usage
            )
            await db.commit()

        # 发布结束事件
        await event_bus.publish(AgentEvent(
            eventType=EventType.AGENT_FINISHED,
            sessionId=session_id,
            data=AgentFinishedResponse(reply=stream_result.final_reply)
        ))
        return ai_message

    # 如果遇到打断
    except RunCancelledError as e:
        logger.info("会话运行被打断")
        partial_id = None
        async with SessionLocal() as db:
            # 如果存在记录下来的已经流式输出的部分模型消息，将这部分消息落库作为一条新的模型消息
            if e.streamed_text:
                partial = await messages_crud.add_message(db, session_id, MessageRole.ASSISTANT, e.streamed_text)
                partial_id = partial.id  # 记录下新消息的ID
            await messages_crud.add_message(db, session_id, MessageRole.SYSTEM, CANCEL_MESSAGE)  # 将打断的消息作为系统消息插入
            await sessions_crud.set_has_pending_task(db, session_id, True)  # 任务被打断标记
            await db.commit()
        # 发布打断事件
        await event_bus.publish(AgentEvent(
            eventType=EventType.RUN_CANCELLED,
            sessionId=session_id,
            data=RunCancelledResponse(message=e.message, message_id=partial_id)
        ))
        raise

    except Exception as e:
        logger.exception("会话运行异常")
        # 发布错误事件
        await event_bus.publish(AgentEvent(
            eventType=EventType.ERROR,
            sessionId=session_id,
            data=ErrorResponse(message=str(e))
        ))
        raise

    finally:
        # 将当前会话清出运行队列
        _active_runs.discard(session_id)


async def run_agent_session(session_id: str, content: str, *, user_message_id: str | None = None) -> Message | None:
    """
    运行一轮 Agent 对话
    
    Args:
        session_id: 会话ID
        content: 本轮用户输入
        user_message_id: 重试场景下复用已有的用户消息ID
    """
    _log_token = bind_session_id(session_id)
    generation = get_cancel_generation(session_id)  # 记录下本轮的代际
    register_pending_run(session_id)
    try:
        async with get_session_lock(session_id):
            # 如果排队期间被取消，代际不合直接自行取消
            if generation != get_cancel_generation(session_id):
                raise RunCancelledError()
            return await _run_agent_session_locked(session_id, content, user_message_id=user_message_id)
    finally:
        unbind_session_id(_log_token)
        unregister_pending_run(session_id)


async def resume_agent_session(approval_id: str, decision: ApprovalStatus, scope: ApprovalScope = ApprovalScope.ONE_TIME) -> str | None:
    """审批完成，恢复图的执行"""
    # 先取出审批单拿到会话ID, 用于获取会话锁
    async with SessionLocal() as db:
        approval = await approvals_crud.get_approval_by_id(db, approval_id)  # 拿到审批单
        if approval is None:
            raise ValueError("审批单不存在")
        session_id = approval.session_id
        thread_id = approval.thread_id

    _log_token = bind_session_id(session_id)
    generation = get_cancel_generation(session_id)
    register_pending_run(session_id)
    try:
        async with get_session_lock(session_id):
            if generation != get_cancel_generation(session_id):
                raise RunCancelledError()
            # 清除遗留的取消状态
            cancel_event  = get_cancel_event(session_id)
            cancel_event.clear()
            _active_runs.add(session_id)
            try:
                async with SessionLocal() as db:
                    # 加锁期间二次校验, 防止等待期间审批单已被处理
                    approval = await approvals_crud.get_approval_by_id(db, approval_id)
                    if approval is None:
                        raise ValueError("审批单不存在")
                    if approval.status != ApprovalStatus.PENDING.value:
                        raise ValueError("审批单已处理")

                    # 更新审批单
                    await approvals_crud.update_approval(db, approval_id, decision, scope)
                    await db.commit()

                    logger.info("审批决定: approval_id=%s, decision=%s, scope=%s", approval_id, decision.value, scope.value)

                    # 发布审批结束事件到总线
                    await event_bus.publish(AgentEvent(
                        eventType=EventType.APPROVAL_RESULT,
                        sessionId=session_id,
                        data=ApprovalResultResponse(
                            approval_id=approval.id,
                            status=decision
                        )
                    ))

                    # 恢复图的执行
                    config = {"configurable": {"thread_id": thread_id}}
                    plan_queue = await load_plan_queue(db, session_id)
                    grants = await approvals_crud.get_session_grants(db, session_id)
                    try:
                        stream_result: StreamResult = await process_stream(
                            db, session_id, plan_queue, Command(resume=decision.value, update={"grants": grants.model_dump()}), config, cancel_event
                        )
                    except RunCancelledError:
                        raise
                    except Exception as e:
                        # 出现异常回滚审批单
                        await approvals_crud.revert_approval(db, approval_id)
                        await db.commit()
                        await event_bus.publish(AgentEvent(
                            eventType=EventType.ERROR,
                            sessionId=session_id,
                            data=ErrorResponse(message=f"恢复执行失败: {e}")
                        ))
                        raise

                    # 再次检查是否还有中断
                    if stream_result.interrupt is not None:
                        execution = await tool_executions_crud.create_pending_execution(
                            db, session_id, stream_result.interrupt.tool, stream_result.interrupt.tool_input, stream_result.interrupt.tool_call_id
                        )
                        new_approval = await approvals_crud.create_approval(
                            db, session_id, thread_id, execution.id
                        )
                        await db.commit()
                        logger.info("恢复执行后再次出现审批: approval_id=%s, tool=%s", new_approval.id, stream_result.interrupt.tool)
                        await event_bus.publish(AgentEvent(
                            eventType=EventType.APPROVAL_REQUIRED,
                            sessionId=session_id,
                            data=ApprovalRequiredResponse(
                                approval_id=new_approval.id,
                                tool=stream_result.interrupt.tool,
                                tool_input=stream_result.interrupt.tool_input
                            )
                        ))
                        return None
                    ai_message = await messages_crud.add_message(
                        db, session_id, MessageRole.ASSISTANT, stream_result.final_reply,
                        usage=stream_result.final_usage
                    )
                    await db.commit()

                await event_bus.publish(AgentEvent(
                    eventType=EventType.AGENT_FINISHED,
                    sessionId=session_id,
                    data=AgentFinishedResponse(reply=stream_result.final_reply)
                ))
                return stream_result.final_reply

            except RunCancelledError as e:
                logger.info("恢复执行被打断")
                partial_id = None
                async with SessionLocal() as db:
                    # 如果存在记录下来的已经流式输出的部分模型消息，将这部分消息落库作为一条新的模型消息
                    if e.streamed_text:
                        partial = await messages_crud.add_message(db, session_id, MessageRole.ASSISTANT, e.streamed_text)
                        partial_id = partial.id  # 记录下新消息的ID
                    await messages_crud.add_message(db, session_id, MessageRole.SYSTEM, CANCEL_MESSAGE)  # 将打断的消息作为系统消息插入
                    await sessions_crud.set_has_pending_task(db, session_id, True)  # 设置任务被打断标记
                    await db.commit()
                # 发布打断事件
                await event_bus.publish(AgentEvent(
                    eventType=EventType.RUN_CANCELLED,
                    sessionId=session_id,
                    data=RunCancelledResponse(message=e.message, message_id=partial_id)
                ))
                raise

            finally:
                # 将当前会话清出运行队列
                _active_runs.discard(session_id)
    finally:
        unbind_session_id(_log_token)
        unregister_pending_run(session_id)


async def retry_agent_session(session_id: str, message_id: str, new_content: str | None) -> Message | None:
    """重新运行某条用户消息"""
    _log_token = bind_session_id(session_id)
    generation = get_cancel_generation(session_id)  # 记录入队时的取消ID
    register_pending_run(session_id)
    try:
        async with get_session_lock(session_id):
            if generation != get_cancel_generation(session_id):
                raise RunCancelledError()
            
            async with SessionLocal() as db:
                message = await messages_crud.get_message_by_id(db, message_id)
                # 检查此消息是否存在
                if message is None or message.session_id != session_id:
                    raise ValueError("消息不存在")
                # 检查是否是用户消息
                if message.role != MessageRole.USER.value:
                    raise ValueError("非用户消息无法重试")
                # 检查后续是否有正常对话
                if await messages_crud.has_user_message_after(db, session_id, message.created_at):
                    raise ValueError("该消息后存在新对话, 无法重试")

                # 检查消息是否被重新编辑了，重新编辑了才采用新消息，否则沿用旧消息
                content = new_content if new_content is not None else message.content
                # 清理此消息之后的残留
                await messages_crud.delete_messages_after(db, session_id, message.created_at)
                await approvals_crud.delete_approval_after(db, session_id, message.created_at)
                await tool_executions_crud.delete_execution_after(db, session_id, message.created_at)
                await sessions_crud.set_context_summary(db, session_id, None, None)
                if new_content is not None:
                    await messages_crud.update_message_content(db, message_id, new_content)
                await db.commit()

            # 重跑消息
            return await _run_agent_session_locked(session_id, content, user_message_id=message_id)
    finally:
        unbind_session_id(_log_token)
        unregister_pending_run(session_id)