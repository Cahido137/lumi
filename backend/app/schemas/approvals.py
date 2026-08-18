from pydantic import BaseModel, Field
from typing import Literal


class ApprovalDecisionRequest(BaseModel):
    """审批决定请求"""
    status: Literal["approved", "rejected"] = Field(..., description="审批决定")
    scope: Literal["one_time", "command", "tool"] = Field("one_time", description="授权范围")