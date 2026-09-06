"""计划步骤的领域模型。"""

from pydantic import BaseModel, Field

from app.schemas.enums import TodoStatus


class TodoItem(BaseModel):
    """一个计划步骤。

    id 与 position 均在计划创建阶段生成, 不属于模型输出内容。
    """

    id: str = Field(..., description="计划步骤唯一ID")
    """计划步骤唯一ID。"""

    title: str = Field(..., description="步骤描述")
    """步骤描述。"""

    status: TodoStatus = Field(TodoStatus.PENDING, description="步骤状态")
    """步骤状态。默认为 PENDING。"""

    position: int = Field(0, description="步骤在计划列表中的序号")
    """步骤在计划列表中的序号。是排序的唯一依据。"""
