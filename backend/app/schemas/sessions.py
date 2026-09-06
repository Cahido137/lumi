"""会话接口的请求与响应模型。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SessionCreateRequest(BaseModel):
    """创建会话请求体。"""

    title: str | None = Field(None, max_length=200, description="会话标题")
    """会话标题。"""


class SessionCreateResponse(BaseModel):
    """会话创建成功响应体。

    Note:
        本模型开启 populate_by_name 与 from_attributes。可由属性名构造, 也可由具备同名属性的对象直接校验生成。
    """

    id: str
    """会话唯一标识。"""

    title: str
    """会话标题。"""

    status: str
    """会话状态。active: 进行中, archived: 已归档。"""

    created_at: datetime = Field(..., alias="createdAt")
    """会话创建时间。"""

    updated_at: datetime = Field(..., alias="updatedAt")
    """会话最近更新时间。"""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)


class SessionSingleResponse(BaseModel):
    """会话列表中的单条记录。

    仅包含标识、标题、时间信息, 不包含会话详细数据。
    """

    id: str
    """会话唯一标识。"""

    title: str
    """会话标题。"""

    created_at: datetime = Field(..., alias="createdAt")
    """会话创建时间。"""

    updated_at: datetime = Field(..., alias="updatedAt")
    """会话最近更新时间。"""


class SessionListResponse(BaseModel):
    """会话列表分页响应体。"""

    items: list[SessionSingleResponse]
    """当前页的会话记录列表。"""

    page: int
    """当前页码。"""

    page_size: int = Field(..., alias="pageSize")
    """每页条数。"""
