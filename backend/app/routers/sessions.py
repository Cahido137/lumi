"""会话相关路由"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.sessions import SessionCreateRequest, SessionCreateResponse, SessionSingleResponse, SessionListResponse
from app.db.session import get_db
from app.db.models import User
from app.crud import sessions as sessions_crud
from app.utils.response import success_response
from app.core.deps import get_current_user


router = APIRouter(prefix="/api/sessions", tags=["sessions"])


DEFAULT_TITLE = "新会话"

@router.post("/create")
async def create_session(request: SessionCreateRequest, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """创建会话请求"""
    session = await sessions_crud.create_session(db, request.title or DEFAULT_TITLE, current_user.id)
    return success_response(
        message="会话创建成功", 
        data=SessionCreateResponse.model_validate(session)
    )

@router.get("/list")
async def list_sessions(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, alias="pageSize", ge=1, le=100, description="每页条数"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """获取分页会话列表"""
    skip = (page - 1) * page_size  # 计算页码
    sessions = await sessions_crud.list_sessions(db, current_user.id, skip, page_size)
    session_list = [
        SessionSingleResponse(id=session.id, title=session.title, createdAt=session.created_at, updatedAt=session.updated_at) 
        for session in sessions
    ]
    return success_response(
        message="会话列表查询成功",
        data=SessionListResponse(items=session_list, page=page, pageSize=page_size)
    )