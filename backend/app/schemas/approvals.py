from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.enums import ApprovalScope, ApprovalStatus


class ApprovalDecisionRequest(BaseModel):
    """审批决定请求"""

    status: Literal[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED] = Field(..., description="审批决定")
    scope: ApprovalScope = Field(ApprovalScope.ONE_TIME, description="授权范围")
