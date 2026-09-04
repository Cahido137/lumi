from typing import Any

from pydantic import BaseModel, Field

from app.schemas.enums import ApprovalStatus
from app.schemas.todos import TodoItem


class BaseEventResponse(BaseModel):
    pass


class AgentStartResponse(BaseEventResponse):
    pass


class AgentFinishedResponse(BaseEventResponse):
    reply: str = Field(..., description="回复")


class ErrorResponse(BaseEventResponse):
    message: str = Field(..., description="错误消息")


class RunCancelledResponse(BaseEventResponse):
    message: str = Field(..., description="中断说明")
    message_id: str | None = Field(None, description="保留的部分回复消息ID, 没有保留为None")


class ToolStartedResponse(BaseEventResponse):
    tool: str = Field(..., description="工具")
    tool_input: dict[str, Any] = Field(..., description="工具输入")


class ToolFinishedResponse(BaseEventResponse):
    tool: str = Field(..., description="工具")
    tool_output: str = Field(..., description="工具输出")


class TokenResponse(BaseEventResponse):
    token: str = Field(..., description="本token内容")


class PlanUpdatedResponse(BaseEventResponse):
    todos: list[TodoItem] = Field(..., description="完整todo列表")


class ApprovalRequiredResponse(BaseEventResponse):
    approval_id: str = Field(..., description="审批ID")
    tool: str = Field(..., description="待审批工具")
    tool_input: dict[str, Any] = Field(..., description="工具输入")


class ApprovalResultResponse(BaseEventResponse):
    approval_id: str = Field(..., description="审批ID")
    status: ApprovalStatus = Field(..., description="审批结果")


class ContextWarningResponse(BaseEventResponse):
    used_tokens: int = Field(..., description="当前已使用的上下文token数")
    max_context_tokens: int = Field(..., description="模型最大上下文token数")
    fraction: float = Field(..., description="当前上下文使用比例")
    message: str = Field(..., description="警告消息")


class ContextCompactedResponse(BaseEventResponse):
    before_tokens: int = Field(..., description="压缩前上下文token数")
    after_tokens: int = Field(..., description="压缩后上下文token数")
    summarized_message_count: int = Field(..., description="被摘要的消息数量")
