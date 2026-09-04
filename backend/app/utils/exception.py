"""全局异常处理器"""

import traceback

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette import status

# 是否启用开发者模式显示日志
DEBUG_MODE = True


async def http_exception_handler(request: Request, exc: HTTPException):
    """业务异常"""
    return JSONResponse(
        status_code=exc.status_code, content={"code": exc.status_code, "message": exc.detail, "data": None}
    )


async def integrity_error_handler(request: Request, exc: IntegrityError):
    """数据完整性约束异常"""
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
    """数据库操作异常"""
    error_data = None
    if DEBUG_MODE:
        error_data = {"error_type": type(exc).__name__, "error_detail": str(exc), "traceback": traceback.format_exc()}

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"code": 500, "message": "数据库操作错误", "data": error_data},
    )


async def general_exception_handler(request: Request, exc: Exception):
    """通用异常处理器"""
    error_data = None
    if DEBUG_MODE:
        error_data = {"error_type": type(exc).__name__, "error_detail": str(exc), "traceback": traceback.format_exc()}

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"code": 500, "message": "服务器内部错误", "data": error_data},
    )
