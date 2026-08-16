"""聊天相关路由"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, BaseMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.graph.builder import build_agent_graph, SYSTEM_PROMPT
from app.crud import messages as messages_crud
from app.crud import sessions as sessions_crud
from app.db.session import get_db
from app.schemas.chat import ChatRequest, ChatResponse, MessageListResponse, MessageSingleResponse
from app.utils.response import success_response


router = APIRouter(prefix="/api/sessions", tags=["chat"])

# 创建 Agent 图单例
_agent_graph = build_agent_graph()


def _to_langchain_messages(rows) -> list[BaseMessage]:
    """将数据库中的消息行转化为langchain风格消息列表"""
    messages: BaseMessage = []
    for row in rows:
        if row.role == "user":
            messages.append(HumanMessage(content=row.content))
        elif row.role == "assistant":
            messages.append(AIMessage(content=row.content))
    return messages


@router.post("/{session_id}/chat")
async def chat(session_id: UUID, request: ChatRequest, db: AsyncSession = Depends(get_db)):
    """收到消息并运行 Agent"""
    # 校验会话ID是否存在
    session = await sessions_crud.get_session_by_id(db, str(session_id))
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 读取会话中的全部消息
    history = _to_langchain_messages(
        await messages_crud.list_message_asc(db, str(session_id))
    )
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + history + [HumanMessage(content=request.content)]

    # 存储传入的用户消息
    await messages_crud.add_message(db, str(session_id), "user", request.content)
    await db.commit()

    # 运行图
    output = await _agent_graph.ainvoke({
        "messages": messages
    })
    # 拿到回答
    reply = output["messages"][-1].content

    # 存储回答消息
    ai_message = await messages_crud.add_message(db, str(session_id), "assistant", reply)

    return success_response(
        message="已回答",
        data=ChatResponse(sessionId=str(session_id), reply=reply, createdAt=ai_message.created_at)
    )

@router.get("/{session_id}/messages")
async def list_messages(
    session_id: UUID,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, alias="pageSize", ge=1, le=100, description="每页条数"),
    db: AsyncSession = Depends(get_db)
):
    """分页获取历史消息列表"""
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