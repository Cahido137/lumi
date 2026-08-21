from pydantic import BaseModel, Field
from typing import Any

from app.schemas.todos import TodoItem
from app.schemas.enums import ApprovalStatus


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