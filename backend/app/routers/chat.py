"""聊天相关路由"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.session_runner import run_agent_session
from app.core.session_runner import RunCancelledError, retry_agent_session, request_cancel_session
from app.core.deps import get_current_user, get_owned_session_or_404
from app.crud import messages as messages_crud
from app.crud import sessions as sessions_crud
from app.db.session import get_db
from app.db.models import User
from app.schemas.chat import ChatRequest, ChatResponse, MessageListResponse, MessageSingleResponse, CancelResponse, RetryRequest
from app.utils.response import success_response


router = APIRouter(prefix="/api/sessions", tags=["chat"])


@router.post("/{session_id}/chat")
async def chat(session_id: UUID, 
               request: ChatRequest, 
               current_user: User = Depends(get_current_user),
               db: AsyncSession = Depends(get_db)
):
    """收到消息并运行 Agent"""
    # 校验会话ID是否存在
    await get_owned_session_or_404(db, str(session_id), current_user)

    # 运行一轮 Agent
    try:
        ai_message = await run_agent_session(str(session_id), request.content)
    except RunCancelledError as e:
        return success_response(message=e.message, data=None)
    except ValueError as e:
        # 捕获会话运行器中锁守卫的异常
        raise HTTPException(status_code=409, detail=str(e))
    
    # 如果没有返回，说明此处中断了
    if ai_message is None:
        return success_response(
            message="任务暂停, 等待人工审批",
            data=ChatResponse(sessionId=str(session_id), reply="任务暂停, 等待人工审批", createdAt=None)
        )

    return success_response(
        message="已回答",
        data=ChatResponse(
            sessionId=str(session_id),
            reply=ai_message.content,
            createdAt=ai_message.created_at
        )
    )

@router.get("/{session_id}/messages")
async def list_messages(
    session_id: UUID,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, alias="pageSize", ge=1, le=100, description="每页条数"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """分页获取历史消息列表"""
    await get_owned_session_or_404(db, str(session_id), current_user)
    skip = (page - 1) * page_size
    messages = await messages_crud.list_messages(db, str(session_id), skip, page_size)
    message_list = [
        MessageSingleResponse(id=m.id, role=m.role, content=m.content, createdAt=m.created_at) 
        for m in messages
    ]
    return success_response(
        message="查询消息列表成功",
        data=MessageListResponse(items=message_list, page=page, pageSize=page_size)
    )

@router.post("/{session_id}/cancel")
async def cancel_run(
    session_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """打断当前正在运行的对话"""
    await get_owned_session_or_404(db, str(session_id), current_user)
    cancelled = request_cancel_session(str(session_id))  # 发送打断请求
    return success_response(
        message="已发送打断请求" if cancelled else "当前没有正在运行的对话",
        data=CancelResponse(sessionId=str(session_id), cancelled=cancelled)
    )

@router.post("/{session_id}/messages/{message_id}/retry")
async def retry_message(
    session_id: UUID, 
    message_id: UUID, 
    request: RetryRequest, 
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """重新运行对话"""
    await get_owned_session_or_404(db, str(session_id), current_user)
    try:
        ai_message = await retry_agent_session(str(session_id), str(message_id), request.content)  # 重新运行
    except RunCancelledError as e:
        return success_response(message=e.message, data=None)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if ai_message is None:
        return success_response(message="任务暂停, 等待人工审批", data=None)
    return success_response(
        message="已重新运行对话",
        data=ChatResponse(sessionId=str(session_id), reply=ai_message.content, createdAt=ai_message.created_at)
    )