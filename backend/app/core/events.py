"""运行事件"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field, ConfigDict, SerializeAsAny

from app.core.event_response import BaseEventResponse
from app.schemas.enums import EventType


class AgentEvent(BaseModel):
    """事件结构封装"""
    event_type: EventType = Field(..., alias="eventType", description="事件类型")
    session_id: str = Field(..., alias="sessionId", description="所属会话ID")
    data: SerializeAsAny[BaseEventResponse] = Field(default_factory=BaseEventResponse, description="事件数据载体")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="事件生成时间戳")

    model_config = ConfigDict(
        populate_by_name=True
    )