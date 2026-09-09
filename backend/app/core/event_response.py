"""事件载荷的数据结构。"""

from typing import Any

from pydantic import BaseModel, Field

from app.schemas.enums import ApprovalStatus
from app.schemas.todos import TodoItem


class BaseEventResponse(BaseModel):
    """全部事件载荷的公共基类。

    Note:
        本类不含任何字段, 所有事件类均应继承本类。
    """


class AgentStartResponse(BaseEventResponse):
    """运行开始事件的载荷。

    Note:
        本类不含任何字段, 但也作为一个独立的载荷结构。
    """


class AgentFinishedResponse(BaseEventResponse):
    """运行正常结束事件的载荷。"""

    reply: str = Field(..., description="回复")


class ErrorResponse(BaseEventResponse):
    """运行出错事件的载荷。"""

    message: str = Field(..., description="错误消息")


class RunCancelledResponse(BaseEventResponse):
    """运行被人工打断事件的载荷。"""

    message: str = Field(..., description="中断说明")
    message_id: str | None = Field(None, description="保留的部分回复消息ID, 没有保留为None")


class ToolStartedResponse(BaseEventResponse):
    """工具开始执行事件的载荷。"""

    tool: str = Field(..., description="工具")
    tool_input: dict[str, Any] = Field(..., description="工具输入")


class ToolFinishedResponse(BaseEventResponse):
    """工具结束执行事件的载荷。"""

    tool: str = Field(..., description="工具")
    tool_output: str = Field(..., description="工具输出")


class TokenResponse(BaseEventResponse):
    """流式输出增量文本事件的载荷。"""

    token: str = Field(..., description="本token内容")


class PlanUpdatedResponse(BaseEventResponse):
    """计划表变更事件的载荷。"""

    todos: list[TodoItem] = Field(..., description="完整todo列表")


class ApprovalRequiredResponse(BaseEventResponse):
    """工具调用等待人工审批事件的载荷。"""

    approval_id: str = Field(..., description="审批ID")
    tool: str = Field(..., description="待审批工具")
    tool_input: dict[str, Any] = Field(..., description="工具输入")


class ApprovalResultResponse(BaseEventResponse):
    """人工审批做出决定事件的载荷。"""

    approval_id: str = Field(..., description="审批ID")
    status: ApprovalStatus = Field(..., description="审批结果")


class ContextWarningResponse(BaseEventResponse):
    """上下文用量达到警告阈值事件的载荷。"""

    used_tokens: int = Field(..., description="当前已使用的上下文token数")
    max_context_tokens: int = Field(..., description="模型最大上下文token数")
    fraction: float = Field(..., description="当前上下文使用比例")
    message: str = Field(..., description="警告消息")


class ContextCompactedResponse(BaseEventResponse):
    """上下文压缩完成事件的载荷。"""

    before_tokens: int = Field(..., description="压缩前上下文token数")
    after_tokens: int = Field(..., description="压缩后上下文token数")
    summarized_message_count: int = Field(..., description="被摘要的消息数量")
