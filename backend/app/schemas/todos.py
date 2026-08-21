from pydantic import BaseModel, Field

from app.schemas.enums import TodoStatus


class TodoItem(BaseModel):
    """一个计划步骤"""
    id: str = Field(..., description="计划步骤唯一ID")
    title: str = Field(..., description="步骤描述")
    status: TodoStatus = Field(TodoStatus.PENDING, description="步骤状态")
    position: int = Field(0, description="步骤在计划列表中的序号")