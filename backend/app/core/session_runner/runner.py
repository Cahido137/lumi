"""会话运行器"""

import logging

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.core.event_bus import event_bus
from app.core.event_response import (
    AgentFinishedResponse,
    AgentStartResponse,
    ApprovalRequiredResponse,
    ApprovalResultResponse,
    ErrorResponse,
    RunCancelledResponse,
)
from app.core.events import AgentEvent
from app.core.logging_config import bind_session_id, unbind_session_id
from app.core.plan_queue import PlanQueue
from app.core.prompts import get_system_messages
from app.core.session_runner.context import StreamResult
from app.core.session_runner.helpers import build_config, load_plan_queue, rebuild_history
from app.core.session_runner.state import (
    CANCEL_MESSAGE,
    RunCancelledError,
    _active_runs,
    get_cancel_event,
    get_cancel_generation,
    get_session_lock,
    register_pending_run,
    unregister_pending_run,
)
from app.core.session_runner.stream import process_stream
from app.crud import approvals as approvals_crud
from app.crud import messages as messages_crud
from app.crud import runs as runs_crud
from app.crud import sessions as sessions_crud
from app.crud import todos as todos_crud
from app.crud import tool_executions as tool_executions_crud
from app.db.models import Message
from app.db.session import SessionLocal
from app.schemas.enums import (
    ApprovalScope,
    ApprovalStatus,
    EventType,
    MessageRole,
    RunStatus,
)
from app.schemas.error_code import CommonErrorCode
from app.schemas.todos import TodoItem, TodoStatus
from app.utils.errors import ConflictError, Error

logger = logging.getLogger(__name__)


def _error_code_of(exc: BaseException) -> str:
    """提取出异常中包含的稳定错误码。

    Args:
        exc: 捕获的异常。

    Returns:
        str: 捕获的业务异常中自带的稳定错误码, 如果没有则视为 internal_error。
    """
    if isinstance(exc, Error):
        return str(exc.error_code)  # 提取出业务异常中的稳定错误码
    return str(CommonErrorCode.INTERNAL_ERROR)


async def _finalize_run(run_id: str | None, outcome: RunStatus | None, error_code: str | None = None) -> None:
    """按运行结果收尾一条运行记录。

    Args:
        run_id: 运行记录ID, 从未登记为 None。
        outcome: 运行结果状态, 为 None 时表示无需收尾。
        error_code: 失败或退回等待审批时的错误码。
    """
    # 还没有运行记录或无需收尾
    if run_id is None or outcome is None:
        return
    try:
        async with SessionLocal() as db:
            if outcome is RunStatus.SUCCEEDED:
                await runs_crud.mark_run_succeeded(db, run_id)
            elif outcome is RunStatus.FAILED:
                await runs_crud.mark_run_failed(
                    db, run_id, error_code=error_code or str(CommonErrorCode.INTERNAL_ERROR)
                )
            elif outcome is RunStatus.CANCELLED:
                await runs_crud.mark_run_cancelled(db, run_id)
            elif outcome is RunStatus.WAITING_APPROVAL:
                await runs_crud.mark_run_waiting_approval(db, run_id, error_code=error_code)
            await db.commit()
    except Exception:
        logger.exception("运行状态收尾失败: run_id=%s, outcome=%s", run_id, outcome)


async def _run_agent_session_locked(
    session_id: str, content: str, *, user_message_id: str | None = None, attempt: int = 1
) -> Message | None:
    """执行一轮 Agent 对话。调用方必须已经持有会话锁并通过取消代际校验。

    Args:
        session_id: 会话ID。
        content: 用户输入消息内容。
        user_message_id: 重试场景下复用已有的用户消息ID。
        attempt: 同一条输入消息的第几次尝试。
    """
    # 清除上一次遗留的取消状态
    cancel_event = get_cancel_event(session_id)
    cancel_event.clear()
    _active_runs.add(session_id)  # 将本会话加入运行队列
    logger.info("开始执行会话轮次: user_message_id=%s", user_message_id or " - ")

    run_id: str | None = None
    outcome: RunStatus | None = None
    run_error_code: str | None = None

    try:
        # 发布开始事件
        await event_bus.publish(
            AgentEvent(event_type=EventType.AGENT_STARTED, session_id=session_id, data=AgentStartResponse())
        )

        # 存在待处理的审批禁止开始新一轮的对话
        async with SessionLocal() as db:
            if await approvals_crud.has_pending_approval(db, session_id):
                raise ValueError("该会话存在未完成的审批, 请先完成审批再开始新对话")

        async with SessionLocal() as db:
            if user_message_id is None:
                user_row = await messages_crud.add_message(db, session_id, MessageRole.USER, content)
                await db.commit()
                user_message_id = user_row.id

            run_context = build_config(session_id)  # 创建配置
            # 登记本轮运行
            run = await runs_crud.create_run(
                db,
                session_id,
                run_context.thread_id,
                input_message_id=user_message_id,
                attempt=attempt,
            )
            run_id = run.id
            await runs_crud.mark_run_started(db, run_id)
            await db.commit()
            logger.info("运行已登记: run_id=%s, attempt=%d", run_id, attempt)

            # 重建消息历史
            history = await rebuild_history(db, session_id, exclude_id=user_message_id)
            messages = get_system_messages() + history + [HumanMessage(content=content, id=user_message_id)]

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
                db=db,
                session_id=session_id,
                plan_queue=PlanQueue(),
                graph_input=graph_input,
                config=run_context.config,
                cancel_event=cancel_event,
            )

            # 如果有中断信息则创建审批相关信息并落库，并且发布审批事件到总线
            if stream_result.interrupt is not None:
                execution = await tool_executions_crud.create_pending_execution(
                    db,
                    session_id,
                    stream_result.interrupt.tool,
                    stream_result.interrupt.tool_input,
                    stream_result.interrupt.tool_call_id,
                )
                approval = await approvals_crud.create_approval(db, session_id, run_context.thread_id, execution.id)
                await db.commit()
                logger.info("工具待审批: approval_id=%s, tool=%s", approval.id, stream_result.interrupt.tool)
                await event_bus.publish(
                    AgentEvent(
                        event_type=EventType.APPROVAL_REQUIRED,
                        session_id=session_id,
                        data=ApprovalRequiredResponse(
                            approval_id=approval.id,
                            tool=stream_result.interrupt.tool,
                            tool_input=stream_result.interrupt.tool_input,
                        ),
                    )
                )
                outcome = RunStatus.WAITING_APPROVAL  # 设定运行状态为等待审批
                return None

            # 最后回答
            ai_message = await messages_crud.add_message(
                db, session_id, MessageRole.ASSISTANT, stream_result.final_reply, usage=stream_result.final_usage
            )
            await db.commit()

        # 发布结束事件
        await event_bus.publish(
            AgentEvent(
                event_type=EventType.AGENT_FINISHED,
                session_id=session_id,
                data=AgentFinishedResponse(reply=stream_result.final_reply),
            )
        )
        outcome = RunStatus.SUCCEEDED  # 设定运行状态为运行成功结束
        return ai_message

    # 如果遇到打断
    except RunCancelledError as e:
        outcome = RunStatus.CANCELLED  # 设定运行状态为被打断
        logger.info("会话运行被打断")
        partial_id = None
        async with SessionLocal() as db:
            # 如果存在记录下来的已经流式输出的部分模型消息，将这部分消息落库作为一条新的模型消息
            if e.streamed_text:
                partial = await messages_crud.add_message(db, session_id, MessageRole.ASSISTANT, e.streamed_text)
                partial_id = partial.id  # 记录下新消息的ID
            await messages_crud.add_message(
                db, session_id, MessageRole.SYSTEM, CANCEL_MESSAGE
            )  # 将打断的消息作为系统消息插入
            await sessions_crud.set_has_pending_task(db, session_id, True)  # 任务被打断标记
            await db.commit()
        # 发布打断事件
        await event_bus.publish(
            AgentEvent(
                event_type=EventType.RUN_CANCELLED,
                session_id=session_id,
                data=RunCancelledResponse(message=e.message, message_id=partial_id),
            )
        )
        raise

    except Exception as e:
        outcome = RunStatus.FAILED  # 设定运行状态为运行异常
        run_error_code = _error_code_of(e)
        logger.exception("会话运行异常")
        # 发布错误事件
        await event_bus.publish(
            AgentEvent(event_type=EventType.ERROR, session_id=session_id, data=ErrorResponse(message=str(e)))
        )
        raise

    finally:
        # 将当前会话清出运行队列
        _active_runs.discard(session_id)
        # 收尾运行记录
        await _finalize_run(run_id, outcome, run_error_code)


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


async def resume_agent_session(
    approval_id: str, decision: ApprovalStatus, scope: ApprovalScope = ApprovalScope.ONE_TIME
) -> str | None:
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
            cancel_event = get_cancel_event(session_id)
            cancel_event.clear()
            _active_runs.add(session_id)

            run_id: str | None = None
            outcome: RunStatus | None = None
            run_error_code: str | None = None

            try:
                async with SessionLocal() as db:
                    # 加锁期间二次校验, 防止等待期间审批单已被处理
                    approval = await approvals_crud.get_approval_by_id(db, approval_id)
                    if approval is None:
                        raise ValueError("审批单不存在")
                    if approval.status != ApprovalStatus.PENDING.value:
                        raise ValueError("审批单已处理")

                    # 领取运行的执行权
                    existing = await runs_crud.get_run_by_thread_id(db, thread_id)
                    # 运行不存在
                    if existing is None:
                        logger.warning("审批恢复找不到对应的运行记录: thread_id=%s", thread_id)
                        raise ConflictError(message="审批恢复对应的运行记录不存在")
                    # 运行无法流转
                    if not await runs_crud.mark_run_started(db, existing.id):
                        logger.warning("运行状态未能流转至 running: run_id=%s", existing.id)
                        raise ConflictError(message="运行状态不允许流转至 running, 恢复图运行失败")
                    run_id = existing.id

                    # 领取成功后记录审批决定
                    await approvals_crud.update_approval(db, approval_id, decision, scope)
                    await db.commit()
                    logger.info(
                        "审批决定: approval_id=%s, decision=%s, scope=%s", approval_id, decision.value, scope.value
                    )

                    # 发布审批结束事件
                    await event_bus.publish(
                        AgentEvent(
                            event_type=EventType.APPROVAL_RESULT,
                            session_id=session_id,
                            data=ApprovalResultResponse(approval_id=approval.id, status=decision),
                        )
                    )

                    # 恢复图的执行
                    config = {"configurable": {"thread_id": thread_id}}
                    plan_queue = await load_plan_queue(db, session_id)
                    grants = await approvals_crud.get_session_grants(db, session_id)
                    try:
                        stream_result: StreamResult = await process_stream(
                            db,
                            session_id,
                            plan_queue,
                            Command(resume=decision.value, update={"grants": grants.model_dump()}),
                            config,
                            cancel_event,
                        )
                    except RunCancelledError:
                        raise
                    except Exception as e:
                        # 出现异常回滚审批单
                        await approvals_crud.revert_approval(db, approval_id)
                        await db.commit()
                        outcome = RunStatus.WAITING_APPROVAL  # 设定运行状态为等待审批
                        run_error_code = _error_code_of(e)  # 记录错误码
                        await event_bus.publish(
                            AgentEvent(
                                event_type=EventType.ERROR,
                                session_id=session_id,
                                data=ErrorResponse(message=f"恢复执行失败: {e}"),
                            )
                        )
                        raise

                    # 再次检查是否还有中断
                    if stream_result.interrupt is not None:
                        execution = await tool_executions_crud.create_pending_execution(
                            db,
                            session_id,
                            stream_result.interrupt.tool,
                            stream_result.interrupt.tool_input,
                            stream_result.interrupt.tool_call_id,
                        )
                        new_approval = await approvals_crud.create_approval(db, session_id, thread_id, execution.id)
                        await db.commit()
                        logger.info(
                            "恢复执行后再次出现审批: approval_id=%s, tool=%s",
                            new_approval.id,
                            stream_result.interrupt.tool,
                        )
                        await event_bus.publish(
                            AgentEvent(
                                event_type=EventType.APPROVAL_REQUIRED,
                                session_id=session_id,
                                data=ApprovalRequiredResponse(
                                    approval_id=new_approval.id,
                                    tool=stream_result.interrupt.tool,
                                    tool_input=stream_result.interrupt.tool_input,
                                ),
                            )
                        )
                        outcome = RunStatus.WAITING_APPROVAL  # 设定运行状态为等待审批
                        return None
                    await messages_crud.add_message(
                        db,
                        session_id,
                        MessageRole.ASSISTANT,
                        stream_result.final_reply,
                        usage=stream_result.final_usage,
                    )
                    await db.commit()

                await event_bus.publish(
                    AgentEvent(
                        event_type=EventType.AGENT_FINISHED,
                        session_id=session_id,
                        data=AgentFinishedResponse(reply=stream_result.final_reply),
                    )
                )
                outcome = RunStatus.SUCCEEDED  # 设定运行状态为运行成功结束
                return stream_result.final_reply

            except RunCancelledError as e:
                outcome = RunStatus.CANCELLED  # 设定运行状态为被打断
                logger.info("恢复执行被打断")
                partial_id = None
                async with SessionLocal() as db:
                    # 如果存在记录下来的已经流式输出的部分模型消息，将这部分消息落库作为一条新的模型消息
                    if e.streamed_text:
                        partial = await messages_crud.add_message(
                            db, session_id, MessageRole.ASSISTANT, e.streamed_text
                        )
                        partial_id = partial.id  # 记录下新消息的ID
                    await messages_crud.add_message(
                        db, session_id, MessageRole.SYSTEM, CANCEL_MESSAGE
                    )  # 将打断的消息作为系统消息插入
                    await sessions_crud.set_has_pending_task(db, session_id, True)  # 设置任务被打断标记
                    await db.commit()
                # 发布打断事件
                await event_bus.publish(
                    AgentEvent(
                        event_type=EventType.RUN_CANCELLED,
                        session_id=session_id,
                        data=RunCancelledResponse(message=e.message, message_id=partial_id),
                    )
                )
                raise

            # 兜底
            except Exception as e:
                if outcome is None:
                    outcome = RunStatus.FAILED
                    run_error_code = _error_code_of(e)
                raise

            finally:
                # 将当前会话清出运行队列
                _active_runs.discard(session_id)
                # 收尾当前运行记录
                await _finalize_run(run_id, outcome, run_error_code)
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
                attempt = await runs_crud.next_attempt(db, session_id, message_id)  # 计算这是同一条消息输入的第几次尝试
                await db.commit()

            # 重跑消息
            return await _run_agent_session_locked(session_id, content, user_message_id=message_id, attempt=attempt)
    finally:
        unbind_session_id(_log_token)
        unregister_pending_run(session_id)
