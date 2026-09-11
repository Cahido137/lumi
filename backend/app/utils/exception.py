"""全局异常处理器函数实现。

全部处理函数返回统一三段式信封, 格式为: {"code", "message", "data"}。

Note:
    本模块仅提供全局异常处理器函数的实现, 不实现注册。
    全部处理函数遵循 Starlette 的 (request, exc) 签名形式。
"""

import logging
import traceback

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette import status

from app.config import get_opssettings
from app.utils.errors import Error

logger = logging.getLogger(__name__)


def _debug_payload(error_type: str, error_detail: str, *, with_traceback: bool = False) -> dict | None:
    """按配置构造响应中的调试载荷。

    Args:
        error_type: 异常类型名。
        error_detail: 原始错误信息。
        with_traceback: 是否附带完整堆栈信息。

    Returns:
        dict | None: 调试关闭时返回 None, 此时响应的 data 字段中不携带任何调试信息。
    """
    if not get_opssettings().debug_error_detail:
        return None
    payload = {"error_type": error_type, "error_detail": error_detail}
    if with_traceback:
        payload["traceback"] = traceback.format_exc()
    return payload


async def business_error_handler(request: Request, exc: Error) -> JSONResponse:
    """处理业务侧抛出的所有业务相关异常。

    Args:
        request: 当前请求。
        exc: 业务异常实例。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}, data 中至少包含 error_code。
    """
    logger.warning("业务异常: path=%s, code=%s, message=%s", request.url.path, exc.error_code, exc.message)
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "code": exc.http_status,
            "message": exc.message,
            "data": exc.to_payload(include_debug=get_opssettings().debug_error_detail),
        },
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """处理业务侧主动抛出的 HTTPException。

    Args:
        request: 当前请求。
        exc: 业务抛出的 HTTP 异常。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}。
    """
    return JSONResponse(
        status_code=exc.status_code, content={"code": exc.status_code, "message": exc.detail, "data": None}
    )


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """处理数据库完整性约束冲突, 统一返回 400。

    Args:
        request: 当前请求。
        exc: SQLAlchemy 完整性约束异常。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}, HTTP 状态码 400。
    """
    error_msg = str(exc.orig)  # 获取错误信息
    if "unique constraint" in error_msg.lower() or "duplicate key" in error_msg.lower():
        detail, error_code = "数据已存在", "duplicate"
    elif "foreign key" in error_msg.lower():
        detail, error_code = "关联数据不存在", "foreign_key_violation"
    else:
        detail, error_code = "数据约束冲突", "integrity_error"
    data: dict = {"error_code": error_code}
    debug = _debug_payload("IntegrityError", error_msg)
    if debug is not None:
        data.update(debug)
    logger.warning("数据库完整性约束冲突: path=%s, code=%s", request.url.path, error_code)
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"code": 400, "message": detail, "data": data})


async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    """处理未被更具体处理函数捕获的数据库相关异常, 统一返回 500。

    Args:
        request: 当前请求。
        exc: SQLAlchemy 异常。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}, HTTP 状态码 500。
    """
    logger.exception("数据库操作异常: path=%s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": 500,
            "message": "数据库操作错误",
            "data": _debug_payload(type(exc).__name__, str(exc), with_traceback=True),
        },
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底处理全部未被捕获的异常, 统一返回 500。

    Args:
        request: 当前请求。
        exc: 所有未被其他处理函数捕获的异常。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}, HTTP 状态码 500。
    """
    logger.exception("未处理的异常: path=%s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "code": 500,
            "message": "服务器内部错误",
            "data": _debug_payload(type(exc).__name__, str(exc), with_traceback=True),
        },
    )
