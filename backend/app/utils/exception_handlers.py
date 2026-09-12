"""全局异常处理函数注册器。

Note:
    Error 的子类会命中 business_error_handler 处理器进行异常处理。
"""

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.utils.errors import Error
from app.utils.exception import (
    business_error_handler,
    general_exception_handler,
    http_exception_handler,
    integrity_error_handler,
    request_validation_handler,
    sqlalchemy_error_handler,
)


def register_exception_handlers(app: FastAPI):
    """注册全局异常处理器。

    Args:
        app: 待注册的 FastAPI 应用实例。
    """
    app.add_exception_handler(Error, business_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, request_validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, general_exception_handler)
