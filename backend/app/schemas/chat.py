"""对话接口的请求与响应模型。"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.enums import MessageRole


class ChatRequest(BaseModel):
    """用户向模型发送消息请求体。"""

    content: str = Field(..., min_length=1, max_length=8000, description="用户输入")
    """用户输入。长度限定为 1-8000 字符。"""


class ChatResponse(BaseModel):
    """对话请求返回的响应体。

    reply 为完整回复内容。
    created_at 允许为 None。

    Note:
        对话被人工打断时不返回本响应体, 信封中的 data 字段直接为 None。
    """

    session_id: str = Field(..., alias="sessionId")
    """会话唯一标识。"""

    reply: str
    """完整回复内容。"""

    created_at: datetime | None = Field(None, alias="createdAt")
    """回复创建时间。等待审批时为 None; 被打断时不返回本响应体, 信封中的 data 字段直接返回 None。"""


class MessageSingleResponse(BaseModel):
    """单条消息的对外表示响应体。

    tool_name 与 tool_call_id 仅在消息角色为 tool 时有值。
    tool_calls 仅在消息角色为 assistant 时有值, 内容为原始工具调用列表。
    """

    id: str
    """消息唯一标识。"""

    role: MessageRole
    """消息角色。"""

    content: str
    """消息正文内容。"""

    tool_name: str | None = Field(None, alias="toolName")
    """工具名称。"""

    tool_call_id: str | None = Field(None, alias="toolCallId")
    """工具调用标识。"""

    tool_calls: list | None = Field(None, alias="toolCalls")
    """工具调用列表。"""

    created_at: datetime = Field(..., alias="createdAt")
    """消息创建时间。"""


class MessageListResponse(BaseModel):
    """消息列表的分页响应体。

    page 为当前页码;
    page_size 为每页条数, 对外字段名为 pageSize。
    """

    items: list[MessageSingleResponse]
    """当前页的消息列表。"""

    page: int
    """当前页码。"""

    page_size: int = Field(..., alias="pageSize")
    """每页条数。"""


class RetryRequest(BaseModel):
    """会话重新运行请求体。

    当 content 为空时表示沿用上次的原始输入重新执行。
    当 content 为非空时表示以编辑后的文本替代原始用户输入后重新执行。
    """

    content: str | None = Field(None, min_length=1, max_length=8000, description="编辑后消息, 没重新编辑为 None")
    """编辑后消息, 没有编辑设置为 None。长度限定为 1-8000 字符。"""


class CancelResponse(BaseModel):
    """中断请求响应体。

    cancelled 表示本次打断请求是否真的中断了会话运行,
    确实中断了返回 True, 如会话没有正在运行的对话则返回 False。
    """

    session_id: str = Field(..., alias="sessionId")
    """会话唯一标识。"""

    cancelled: bool = Field(..., description="是否存在被打断的运行")
    """是否存在被打断的运行。"""
