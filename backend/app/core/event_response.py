from pydantic import BaseModel, Field
from typing import Literal


class BaseEventResponse(BaseModel):
    pass

class AgentStartResponse(BaseEventResponse):
    pass

class AgentFinishedResponse(BaseEventResponse):
    reply: str = Field(..., description="回复")

class ErrorResponse(BaseEventResponse):
    message: str = Field(..., description="错误消息")

class ToolStartedResponse(BaseEventResponse):
    tool: str = Field(..., description="工具")
    tool_input: dict = Field(..., description="工具输入")

class ToolFinishedResponse(BaseEventResponse):
    tool: str = Field(..., description="工具")
    tool_output: str = Field(..., description="工具输出")

class TokenResponse(BaseEventResponse):
    token: str = Field(..., description="本token内容")

class PlanUpdatedResponse(BaseEventResponse):
    todos: list[dict] = Field(..., description="完整todo列表")

class ApprovalRequiredResponse(BaseEventResponse):
    approval_id: str = Field(..., description="审批ID")
    tool: str = Field(..., description="待审批工具")
    tool_input: dict = Field(..., description="工具输入")

class ApprovalResultResponse(BaseEventResponse):
    approval_id: str = Field(..., description="审批ID")
    status: Literal["approved", "rejected"] = Field(..., description="审批结果")