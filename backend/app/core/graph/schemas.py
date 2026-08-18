from pydantic import BaseModel, Field


class PlanItem(BaseModel):
    """计划中的一个步骤"""
    title: str = Field(..., description="一个具体可执行的步骤描述")

class PlanOutput(BaseModel):
    """planner的结构化输出"""
    todos: list[PlanItem] = Field(..., description="计划执行列表")