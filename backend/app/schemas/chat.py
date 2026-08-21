from pydantic import BaseModel, Field
from datetime import datetime

from app.schemas.enums import MessageRole


class ChatRequest(BaseModel):
    """用户消息发送请求"""
    content: str = Field(..., min_length=1, max_length=8000, description="用户输入")

class ChatResponse(BaseModel):
    """聊天响应体"""
    session_id: str = Field(..., alias="sessionId")
    reply: str
    created_at: datetime = Field(None, alias="createdAt")

class MessageSingleResponse(BaseModel):
    """单条消息回复"""
    id: str
    role: MessageRole
    content: str
    created_at: datetime = Field(..., alias="createdAt")

class MessageListResponse(BaseModel):
    """消息列表响应体"""
    items: list[MessageSingleResponse]
    page: int
    page_size: int = Field(..., alias="pageSize")