from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.enums import MessageRole


class ChatRequest(BaseModel):
    """用户消息发送请求"""

    content: str = Field(..., min_length=1, max_length=8000, description="用户输入")


class ChatResponse(BaseModel):
    """聊天响应体"""

    session_id: str = Field(..., alias="sessionId")
    reply: str
    created_at: datetime | None = Field(None, alias="createdAt")


class MessageSingleResponse(BaseModel):
    """单条消息回复"""

    id: str
    role: MessageRole
    content: str
    tool_name: str | None = Field(None, alias="toolName")
    tool_call_id: str | None = Field(None, alias="toolCallId")
    tool_calls: list | None = Field(None, alias="toolCalls")
    created_at: datetime = Field(..., alias="createdAt")


class MessageListResponse(BaseModel):
    """消息列表响应体"""

    items: list[MessageSingleResponse]
    page: int
    page_size: int = Field(..., alias="pageSize")


class RetryRequest(BaseModel):
    """重新运行请求"""

    content: str | None = Field(None, min_length=1, max_length=8000, description="编辑后消息, 没重新编辑为 None")


class CancelResponse(BaseModel):
    """打断请求响应"""

    session_id: str = Field(..., alias="sessionId")
    cancelled: bool = Field(..., description="是否存在被打断的运行")
