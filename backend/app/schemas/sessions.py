from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime


class SessionCreateRequest(BaseModel):
    """创建会话请求体"""
    title: str | None = Field(None, max_length=200, description="会话标题")

class SessionCreateResponse(BaseModel):
    """会话创建响应体"""
    id: str
    title: str
    status: str
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")

    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True
    )

class SessionSingleResponse(BaseModel):
    """单个会话单元"""
    id: str
    title: str
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")

class SessionListResponse(BaseModel):
    """会话列表响应体"""
    items: list[SessionSingleResponse]
    page: int
    page_size: int = Field(..., alias="pageSize")