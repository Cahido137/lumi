"""全局异常处理器函数实现。

全部处理函数返回统一三段式信封, 格式为: {"code", "message", "data"}。

Note:
    本模块仅提供全局异常处理器函数的实现, 不实现注册。
    全部处理函数遵循 Starlette 的 (request, exc) 签名形式。
"""

import traceback

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette import status

DEBUG_MODE = True
"""调试开关。启用后会将原始错误信息和完整的堆栈信息随错误返回给客户端。

Note:
    正式发布时应置为 False, 否则完整堆栈信息会随响应返回客户端, 导致敏感信息泄露。
"""


async def http_exception_handler(request: Request, exc: HTTPException):
    """处理业务侧主动抛出的 HTTPException。

    Args:
        exc: 业务抛出的 HTTP 异常。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}。
    """
    return JSONResponse(
        status_code=exc.status_code, content={"code": exc.status_code, "message": exc.detail, "data": None}
    )


async def integrity_error_handler(request: Request, exc: IntegrityError):
    """处理数据库完整性约束冲突, 统一返回 400。

    Args:
        exc: SQLAlchemy 完整性约束异常。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}, HTTP 状态码 400。
    """
    error_msg = str(exc.orig)  # 获取错误信息
    if "unique constraint" in error_msg.lower() or "duplicate key" in error_msg.lower():
        detail = "数据已存在"
    elif "foreign key" in error_msg.lower():
        detail = "关联数据不存在"
    else:
        detail = "数据约束冲突"

    error_data = None
    if DEBUG_MODE:
        error_data = {"error_type": "IntegrityError", "error_detail": error_msg}

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST, content={"code": 400, "message": detail, "data": error_data}
    )


async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError):
    """处理未被更具体处理函数捕获的数据库相关异常, 统一返回 500。

    Args:
        exc: SQLAlchemy 异常。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}, HTTP 状态码 500。
    """
    error_data = None
    if DEBUG_MODE:
        error_data = {"error_type": type(exc).__name__, "error_detail": str(exc), "traceback": traceback.format_exc()}

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"code": 500, "message": "数据库操作错误", "data": error_data},
    )


async def general_exception_handler(request: Request, exc: Exception):
    """兜底处理全部未被捕获的异常, 统一返回 500。

    Args:
        exc: 所有未被其他处理函数捕获的异常。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}, HTTP 状态码 500。
    """
    error_data = None
    if DEBUG_MODE:
        error_data = {"error_type": type(exc).__name__, "error_detail": str(exc), "traceback": traceback.format_exc()}

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"code": 500, "message": "服务器内部错误", "data": error_data},
    )
