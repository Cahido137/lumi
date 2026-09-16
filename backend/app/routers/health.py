"""后端健康检查路由。

Note:
    存活与就绪探针不做用户鉴权; 深度体检会触发一次真实模型调用, 因此需要用户鉴权。
"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_healthsettings
from app.core import diagnostics
from app.core.deps import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.schemas.health import (
    CheckItem,
    LivenessResponse,
    ReadinessChecks,
    ReadinessResponse,
)
from app.utils.errors import ForbiddenError
from app.utils.response import success_response

router = APIRouter(prefix="/api/health", tags=["health"])


logger = logging.getLogger(__name__)


@router.get("")
async def liveness():
    """存活探针, 用于确认后端路由服务是否还能处理请求。

    Returns:
        JSONResponse: 三段式响应信封, 消息载荷为 LivenessResponse 响应体。
    """
    # 仅确认 FastAPI 服务是否还可以处理请求，因此直接返回
    return success_response(message="Server Online.", data=LivenessResponse())


@router.get("/ready")
async def readiness(db: AsyncSession = Depends(get_db)):
    """就绪探针, 用于检查数据库连接是否可用, 不可用时返回 503 错误码。

    Returns:
        JSONResponse: 三段式响应信封, 消息载荷为 ReadinessResponse 响应体。

    Raises:
        HTTPException 503: 数据库不可达。
    """
    started = time.monotonic()
    # 发送一条数据库查询语句检查是否可以被执行
    try:
        await db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("Database disconnected.", exc_info=True)
        raise HTTPException(status_code=503, detail="Database disconnected.") from None
    latency_ms = round((time.monotonic() - started) * 1000, 2)
    return success_response(
        message="Database connected.",
        data=ReadinessResponse(
            checks=ReadinessChecks(database=CheckItem.passed(detail="Database connected.", latency_ms=latency_ms))
        ),
    )


@router.get("/deep")
async def deep_check(current_user: User = Depends(get_current_user)):
    """深度体检, 包括数据库可达性检查、检查点可达性检查、供应商连通性检查、模型生成链路检查。

    Args:
        current_user: 当前登录用户, 用于鉴权。

    Returns:
        JSONResponse: 三段式响应信封, 消息载荷为 DeepCheckResponse 响应体, 始终返回 HTTP 200。

    Raises:
        HTTPException 401: 未携带令牌、令牌失效或用户不存在。
        ForbiddenError 403: 深度体检在配置层面被关闭。
    """
    if not get_healthsettings().health_deep_enabled:
        raise ForbiddenError("深度体检已关闭")
    data = await diagnostics.deep_check()
    return success_response(message="Deep check finished.", data=data)
