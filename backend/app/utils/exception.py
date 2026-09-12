"""全局异常处理器函数实现。

全部处理函数返回统一三段式信封, 格式为: {"code", "message", "data"}。

Note:
    本模块仅提供全局异常处理器函数的实现, 不实现注册。
    全部处理函数遵循 Starlette 的 (request, exc) 签名形式。
"""

import logging
import traceback

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette import status

from app.config import get_opssettings
from app.utils.errors import Error

logger = logging.getLogger(__name__)

MAX_FIELD_ERRORS = 50
"""单次校验失败响应中返回的最多字段错误数。

Note:
    避免过多的错误字段让响应体的体积失控。
    超出部分只体现在 error_count 和 truncated 字段上, 多余的错误字段会被截断。
"""


def _debug_enabled() -> bool:
    """读取当前是否允许在错误响应中附带调试信息。

    Returns:
        bool: OpsSettings.debug_error_detail。
    """
    return get_opssettings().debug_error_detail


def _debug_payload(error_type: str, error_detail: str, *, with_traceback: bool = False) -> dict | None:
    """按配置构造响应中的调试载荷。

    Args:
        error_type: 异常类型名。
        error_detail: 原始错误信息。
        with_traceback: 是否附带完整堆栈信息。

    Returns:
        dict | None: 调试关闭时返回 None, 此时响应的 data 字段中不携带任何调试信息。
    """
    if not _debug_enabled():
        return None
    payload = {"error_type": error_type, "error_detail": error_detail}
    if with_traceback:
        payload["traceback"] = traceback.format_exc()
    return payload


def _format_loc(loc) -> str:
    """把 pydantic 的错误位置归一化为带点号的路径值。

    Args:
        loc: pydantic 错误中的 loc 字段。

    Returns:
        str: 归一化后的带点号路径值, loc 为空时返回 "(unknown)"。
    """
    if not loc:
        return "(unknown)"
    # 进行归一化
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        elif parts:
            parts.append(f".{item}")
        else:
            parts.append(str(item))
    return "".join(parts)


def _field_errors(exc: RequestValidationError) -> tuple[list[dict[str, str]], int, bool]:
    """从校验异常中提取出可以安全回传的字段。

    Args:
        exc: FastAPI 抛出的请求校验异常。

    Returns:
        tuple[list[dict[str, str]], int, bool]: (截断后的字段错误列表, 真实错误数, 是否发生了错误列表截断)。

    Note:
        本函数会只提取出 loc / type / msg 字段, 抛弃 input 与 ctx 字段。
    """
    errors = [err for err in (exc.errors() or []) if isinstance(err, dict)]  # 提取出完整的错误列表
    total = len(errors)  # 计算真实错误数
    # 拼接出截断后的字段错误列表
    fields = [
        {
            "loc": _format_loc(err.get("loc")),
            "type": str(err.get("type") or "value_error"),
            "msg": str(err.get("msg") or ""),
        }
        for err in errors[:MAX_FIELD_ERRORS]
    ]
    return fields, total, total > MAX_FIELD_ERRORS


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
            "data": exc.to_payload(include_debug=_debug_enabled()),
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


async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """处理请求体、查询参数与路径参数的校验失败异常, 统一返回 422。

    Args:
        request: 当前请求。
        exc: FastAPI 抛出的请求校验异常。

    Returns:
        JSONResponse: 三段式响应信封 {"code", "message", "data"}, HTTP 状态码 422。
        data 中固定携带:
            error_code: "validation_error"。
            fields: 字段级错误列表, 含 loc / type / msg。
            error_count: 真实的错误数。
            truncated: fields 是否被截断。
    """
    fields, error_count, truncated = _field_errors(exc)
    data: dict = {
        "error_code": "validation_error",
        "fields": fields,
        "error_count": error_count,
        "truncated": truncated,
    }
    if _debug_enabled():
        # 仅在调试模式开启的状态下返回明文错误数据
        data["raw_errors"] = jsonable_encoder(exc.errors())
    logger.warning("请求校验失败: path=%s, error_count=%d", request.url.path, error_count)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"code": 422, "message": "请求参数校验失败", "data": data},
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
