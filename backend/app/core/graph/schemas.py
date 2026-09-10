"""图内部使用的结构化模型。"""

from typing import Any

from pydantic import BaseModel, Field


class PlanItem(BaseModel):
    """计划中的一个步骤。"""

    title: str = Field(..., description="一个具体可执行的步骤描述")


class PlanOutput(BaseModel):
    """规划器的结构化输出。

    Note:
        计划列表为空列表表示无需分步或沿用旧计划。
    """

    todos: list[PlanItem] = Field(..., description="计划执行列表")


class ApprovalInterrupt(BaseModel):
    """审批中断载荷, 用于 interrupt() 抛出与恢复时传递。

    Note:
        此结构是审批节点与会话运行器之间的契约: 运行器从图流中取出 "__interrupt__" 字段
        解析本结构并落库审批单, 人工做出决定后再以 Command(resume=...) 回传。
    """

    tool: str = Field(..., description="待审批工具名")
    tool_input: dict[str, Any] = Field(default_factory=dict, description="工具入参")
    tool_call_id: str = Field(..., description="发起工具调用的tool_call_id")
