"""会话运行器"""

from langchain_core.messages import HumanMessage, SystemMessage
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
from app.core.graph.builder import SYSTEM_PROMPT
from app.core.plan_queue import PlanQueue
from app.core.session_runner.context import StreamResult
from app.core.session_runner.helpers import build_config, load_plan_queue, to_langchain_messages
from app.core.session_runner.state import get_session_lock
from app.core.session_runner.stream import process_stream
from app.crud import approvals as approvals_crud
from app.crud import messages as messages_crud
from app.crud import tool_executions as tool_executions_crud
from app.db.models import Message
from app.db.session import SessionLocal
from app.schemas.enums import (
    ApprovalScope,
    ApprovalStatus,
    EventType,
    MessageRole,
)


async def run_agent_session(session_id: str, content: str) -> Message | None:
    """运行一轮 Agent 对话"""
    async with get_session_lock(session_id):
        # 发布开始事件
        await event_bus.publish(AgentEvent(
            eventType=EventType.AGENT_STARTED,
            sessionId=session_id,
            data=AgentStartResponse()
        ))

        try:
            # 存在待处理的审批禁止开始新一轮的对话
            async with SessionLocal() as db:
                if await approvals_crud.has_pending_approval(db, session_id):
                    raise ValueError("该会话存在未完成的审批, 请先完成审批再开始新对话")

            async with SessionLocal() as db:
                # 拼接消息
                history = to_langchain_messages(
                    await messages_crud.list_message_asc(db, session_id)
                )
                messages = [SystemMessage(content=SYSTEM_PROMPT)] + history + [HumanMessage(content=content)]

                # 用户消息落库
                await messages_crud.add_message(db, session_id, MessageRole.USER, content)
                await db.commit()

                run_context = build_config(session_id)  # 创建配置
                grants = await approvals_crud.get_session_grants(db, session_id)  # 获取当前会话工具授权
                stream_result: StreamResult = await process_stream(
                    db=db, session_id=session_id, plan_queue=PlanQueue(),
                    graph_input={"messages": messages, "grants": grants.model_dump()},
                    config=run_context.config
                )

                # 如果有中断信息则创建审批相关信息并落库，并且发布审批事件到总线
                if stream_result.interrupt is not None:
                    execution = await tool_executions_crud.create_pending_execution(
                        db, session_id, stream_result.interrupt.tool, stream_result.interrupt.tool_input
                    )
                    approval = await approvals_crud.create_approval(
                        db, session_id, run_context.thread_id, execution.id
                    )
                    await db.commit()
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
                ai_message = await messages_crud.add_message(db, session_id, MessageRole.ASSISTANT, stream_result.final_reply)
                await db.commit()

            # 发布结束事件
            await event_bus.publish(AgentEvent(
                eventType=EventType.AGENT_FINISHED,
                sessionId=session_id,
                data=AgentFinishedResponse(reply=stream_result.final_reply)
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


async def resume_agent_session(approval_id: str, decision: ApprovalStatus, scope: ApprovalScope = ApprovalScope.ONE_TIME) -> str | None:
    """审批完成，恢复图的执行"""
    # 先取出审批单拿到会话ID, 用于获取会话锁
    async with SessionLocal() as db:
        approval = await approvals_crud.get_approval_by_id(db, approval_id)  # 拿到审批单
        if approval is None:
            raise ValueError("审批单不存在")
        session_id = approval.session_id
        thread_id = approval.thread_id

    async with get_session_lock(session_id):
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
                    db, session_id, plan_queue, Command(resume=decision.value, update={"grants": grants.model_dump()}), config
                )
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
                    db, session_id, stream_result.interrupt.tool, stream_result.interrupt.tool_input
                )
                new_approval = await approvals_crud.create_approval(
                    db, session_id, thread_id, execution.id
                )
                await db.commit()
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
                db, session_id, MessageRole.ASSISTANT, stream_result.final_reply
            )
            await db.commit()

        await event_bus.publish(AgentEvent(
            eventType=EventType.AGENT_FINISHED,
            sessionId=session_id,
            data=AgentFinishedResponse(reply=stream_result.final_reply)
        ))
        return stream_result.final_reply