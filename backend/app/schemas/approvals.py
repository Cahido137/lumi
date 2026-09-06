"""人工审批接口的请求模型。"""

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.enums import ApprovalScope, ApprovalStatus


class ApprovalDecisionRequest(BaseModel):
    """人工审批决定的请求体。

    status 仅接受 APPROVED 与 REJECTED 取值。

    Note:
        决定为 REJECTED 时, 相关工具不会被执行, 对应计划步骤置为失败。
    """

    status: Literal[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED] = Field(..., description="审批决定")
    """审批决定。限定为 APPROVED 和 REJECTED。"""

    scope: ApprovalScope = Field(ApprovalScope.ONE_TIME, description="授权范围")
    """授权范围。默认为ONE_TIME。"""
