from pydantic import BaseModel, Field
from typing import Any


class PlanItem(BaseModel):
    """计划中的一个步骤"""
    title: str = Field(..., description="一个具体可执行的步骤描述")

class PlanOutput(BaseModel):
    """planner的结构化输出"""
    todos: list[PlanItem] = Field(..., description="计划执行列表")

class ApprovalInterrupt(BaseModel):
    """审批中断体"""
    tool: str = Field(..., description="待审批工具名")
    tool_input: dict[str, Any] = Field(default_factory=dict, description="工具入参")
    tool_call_id: str = Field(..., description="发起工具调用的tool_call_id")