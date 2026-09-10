"""聊天相关路由。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_owned_session_or_404
from app.core.session_runner import RunCancelledError, request_cancel_session, retry_agent_session, run_agent_session
from app.crud import messages as messages_crud
from app.db.models import User
from app.db.session import get_db
from app.schemas.chat import (
    CancelResponse,
    ChatRequest,
    ChatResponse,
    MessageListResponse,
    MessageSingleResponse,
    RetryRequest,
)
from app.utils.response import success_response

router = APIRouter(prefix="/api/sessions", tags=["chat"])

PAUSE_REPLY = "任务暂停, 等待人工审批"
"""运行中因等待审批而暂停时, 填入 message 与 ChatResponse.reply 字段中的提示文案。"""


@router.post("/{session_id}/chat")
async def chat(
    session_id: UUID,
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """向指定会话发送一条消息, 阻塞等待本轮 Agent 运行结束。

    Returns:
        JSONResponse: 三段式信封, data 载荷为 ChatResponse。

    Raises:
        HTTPException 401: 未登录或令牌无效。
        HTTPException 404: 会话不存在或不属于当前用户。
        HTTPException 409: 该会话存在未完成的审批, 应该先处理审批。
    """
    # 校验会话ID是否存在
    await get_owned_session_or_404(db, str(session_id), current_user)

    # 运行一轮 Agent
    try:
        ai_message = await run_agent_session(str(session_id), request.content)
    except RunCancelledError as e:
        return success_response(message=e.message, data=None)
    except ValueError as e:
        # 捕获会话运行器中的审批前置检查异常: 会话存在未完成审批时禁止开始新一轮对话
        raise HTTPException(status_code=409, detail=str(e)) from e

    # 如果没有返回，说明此处中断了
    if ai_message is None:
        return success_response(
            message=PAUSE_REPLY,
            data=ChatResponse(sessionId=str(session_id), reply=PAUSE_REPLY, createdAt=None),
        )

    return success_response(
        message="已回答",
        data=ChatResponse(sessionId=str(session_id), reply=ai_message.content, createdAt=ai_message.created_at),
    )


@router.get("/{session_id}/messages")
async def list_messages(
    session_id: UUID,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, alias="pageSize", ge=1, le=100, description="每页条数"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """分页获取指定会话的历史消息, 按时间倒序返回。

    Returns:
        JSONResponse: 三段式信封, data 载荷为 MessageListResponse。

    Raises:
        HTTPException 401: 未登录或令牌无效。
        HTTPException 404: 会话不存在或不属于当前用户。
    """
    await get_owned_session_or_404(db, str(session_id), current_user)
    skip = (page - 1) * page_size
    messages = await messages_crud.list_messages(db, str(session_id), skip, page_size)
    message_list = [
        MessageSingleResponse(
            id=m.id,
            role=m.role,
            content=m.content,
            toolName=m.tool_name,
            toolCalls=m.tool_calls,
            toolCallId=m.tool_call_id,
            createdAt=m.created_at,
        )
        for m in messages
    ]
    return success_response(
        message="查询消息列表成功", data=MessageListResponse(items=message_list, page=page, pageSize=page_size)
    )


@router.post("/{session_id}/cancel")
async def cancel_run(
    session_id: UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """打断指定会话当前正在运行的对话。

    Returns:
        JSONResponse: 三段式信封, data 载荷为 CancelResponse。
        cancelled 字段为 False 时表示当前没有正在运行的对话。

    Raises:
        HTTPException 401: 未登录或令牌无效。
        HTTPException 404: 会话不存在或指定会话不属于当前用户。
    """
    await get_owned_session_or_404(db, str(session_id), current_user)
    cancelled = request_cancel_session(str(session_id))  # 发送打断请求
    return success_response(
        message="已发送打断请求" if cancelled else "当前没有正在运行的对话",
        data=CancelResponse(sessionId=str(session_id), cancelled=cancelled),
    )


@router.post("/{session_id}/messages/{message_id}/retry")
async def retry_message(
    session_id: UUID,
    message_id: UUID,
    request: RetryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """以编辑后的内容重新运行指定用户消息及其后续轮次。

    Returns:
        JSONResponse: 三段式信封, data 载荷为 ChatResponse。

    Raises:
        HTTPException 401: 未登录或令牌无效。
        HTTPException 404: 会话或消息不存在、消息非用户消息、此消息后已有新对话。

    Note:
        重试会删除该消息之后的全部消息、审批单与工具执行记录, 并清空会话摘要。
    """
    await get_owned_session_or_404(db, str(session_id), current_user)
    try:
        ai_message = await retry_agent_session(str(session_id), str(message_id), request.content)  # 重新运行
    except RunCancelledError as e:
        return success_response(message=e.message, data=None)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    if ai_message is None:
        return success_response(
            message=PAUSE_REPLY, data=ChatResponse(sessionId=str(session_id), reply=PAUSE_REPLY, createdAt=None)
        )
    return success_response(
        message="已重新运行对话",
        data=ChatResponse(sessionId=str(session_id), reply=ai_message.content, createdAt=ai_message.created_at),
    )
