"""后端健康检查路由。

Note:
    本模块是后端服务健康检查, 访问的时候不做用户鉴权。
"""

import logging
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.checkpoint import ping_checkpoint
from app.core.llm import ping_chat_model, ping_provider
from app.db.session import get_db
from app.schemas.health import (
    CheckItem,
    DeepCheckResponse,
    DeepChecks,
    LivenessResponse,
    ReadinessChecks,
    ReadinessResponse,
)
from app.utils.response import success_response

router = APIRouter(prefix="/api/health", tags=["health"])


logger = logging.getLogger(__name__)


LLM_PING_TIMEOUT = 5.0
"""模型生成功能测试超时时间。"""


def _elapsed_ms(started: float) -> float:
    """计算自 started 起的耗时毫秒数, 保留两位小数。"""
    return round((time.monotonic() - started) * 1000, 2)


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
async def deep_check(db: AsyncSession = Depends(get_db)):
    """深度体检, 包括数据库可达性检查、检查点可达性检查、供应商连通性检查、模型生成链路检查。

    Returns:
        JSONResponse: 三段式响应信封, 消息载荷为 DeepCheckResponse 响应体, 始终返回 HTTP 200。
    """
    # 数据库可达性检查
    started = time.monotonic()
    try:
        await db.execute(text("SELECT 1"))
        database = CheckItem.passed(detail="Database connected.", latency_ms=_elapsed_ms(started))
    except SQLAlchemyError:
        logger.warning("Database disconnected.", exc_info=True)
        database = CheckItem.failed(detail="Database disconnected.", latency_ms=_elapsed_ms(started))

    # 检查点检查
    started = time.monotonic()
    ok, detail = await ping_checkpoint()
    checkpoint = CheckItem(ok=ok, detail=detail, latency_ms=_elapsed_ms(started))

    # 供应商连通性检查
    started = time.monotonic()
    ok, detail = await ping_provider()
    provider = CheckItem(ok=ok, detail=detail, latency_ms=_elapsed_ms(started))

    # 模型生成链路检查
    started = time.monotonic()
    if provider.ok:
        ok, detail = await ping_chat_model(timeout=LLM_PING_TIMEOUT)
        llm_generation = CheckItem(ok=ok, detail=detail, latency_ms=_elapsed_ms(started))
    else:
        llm_generation = CheckItem.failed(detail="Provider unreachable.", latency_ms=_elapsed_ms(started))

    # 汇总
    checks = DeepChecks(database=database, checkpoint=checkpoint, provider=provider, llm_generation=llm_generation)
    failed_names = [name for name, item in checks.model_dump().items() if not item["ok"]]
    status: Literal["healthy", "degraded"] = "degraded" if failed_names else "healthy"
    if failed_names:
        logger.warning("Deep check degraded, failed items: %s", failed_names)
    return success_response(message="Deep check finished.", data=DeepCheckResponse(status=status, checks=checks))
