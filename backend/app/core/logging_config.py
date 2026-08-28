"""日志配置与会话上下文"""

import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar


# 会话ID上下文变量
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)

# 日志输出格式
DEFAULT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | session=%(session_id)s | %(message)s"

class SessionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.session_id = session_id_var.get() or " - "
        return True

def setup_logging(level: str = "INFO") -> None:
    """初始化全局日志配置"""
    handler = logging.StreamHandler(sys.stdout)  # 限制控制台标准输出
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))  # 设置日志格式
    handler.addFilter(SessionFilter())

    root = logging.getLogger()
    root.handlers.clear()  # 清除原本的handler保证幂等性
    root.addHandler(handler)
    root.setLevel(level.upper())  # 设置日志等级

@contextmanager
def session_log_context(session_id: str):
    """把一段代码的日志绑定到指定会话"""
    token = session_id_var.set(session_id)  # 当前协程上下文设置为该会话ID
    try:
        yield
    finally:
        session_id_var.reset(token)

def bind_session_id(session_id: str):
    """设置当前协程上下文的会话ID, 返回token"""
    return session_id_var.set(session_id)

def unbind_session_id(token) -> None:
    """恢复会话ID上下文"""
    session_id_var.reset(token)