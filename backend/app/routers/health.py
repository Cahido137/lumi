"""后端健康检查路由。

Note:
    本模块是后端服务健康检查, 访问的时候不做用户鉴权。
"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.health import CheckItem, LivenessResponse, ReadinessChecks, ReadinessResponse
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
