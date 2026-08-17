"""运行事件"""

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, ConfigDict, SerializeAsAny

from app.core.event_response import BaseEventResponse


class EventType(str, Enum):
    """事件类型枚举"""
    # 生命周期
    AGENT_STARTED = "agent_started"  # data: {}
    AGENT_FINISHED = "agent_finished"  # data: {"reply": 回复}
    ERROR = "error"  # data: {"message": 错误消息}

    # 工具调用相关
    TOOL_STARTED = "tool_started"  # data: {"tool": 工具, "tool_input": 工具输入}
    TOOL_FINISHED = "tool_finished"  # data: {"tool": 工具, "tool_output": 工具输出}

    # 流式输出
    TOKEN = "token"  # data: {"token": 本token内容}

    # todo
    PLAN_UPDATED = "plan_updated"  # data: {"todos": [完整todo列表]}

    # 人工审批相关
    APPROVAL_REQUIRED = "approval_required"  # data: {"approval_id": 审批ID, "tool": 待审批工具, "tool_input": 工具输入}
    APPROVAL_RESULT = "approval_result"  # data: {"approval_id": 审批ID, "status": "approved"/"rejected"}


class AgentEvent(BaseModel):
    """事件结构封装"""
    event_type: EventType = Field(..., alias="eventType", description="事件类型")
    session_id: str = Field(..., alias="sessionId", description="所属会话ID")
    data: SerializeAsAny[BaseEventResponse] = Field(default_factory=BaseEventResponse, description="事件数据载体")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="事件生成时间戳")

    model_config = ConfigDict(
        populate_by_name=True
    )