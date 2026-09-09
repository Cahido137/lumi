"""日志配置与会话上下文。"""

import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar

session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)
"""当前协程上下文中的会话ID, 未绑定时为 None。"""

DEFAULT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | session=%(session_id)s | %(message)s"
"""日志输出格式。

Note:
    其中, session_id 字段由 SessionFilter 注入。
"""


class SessionFilter(logging.Filter):
    """向日志记录注入会话ID字段的过滤器。

    Note:
        本类使用 Filter 的钩子做字段注入。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """为日志注入 session_id 字段。

        Args:
            record: 待处理的日志记录。

        Returns:
            bool: 恒为 True, 本方法只负责注入字段, 不做其他操作。
        """
        record.session_id = session_id_var.get() or " - "
        return True


def setup_logging(level: str = "INFO") -> None:
    """初始化全局日志配置。

    Args:
        level: 日志等级, 默认为 "INFO"。

    Note:
        此函数是幂等的。
    """
    handler = logging.StreamHandler(sys.stdout)  # 输出到标准输出
    handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))  # 设置日志格式
    handler.addFilter(SessionFilter())

    root = logging.getLogger()
    root.handlers.clear()  # 清除原本的handler保证幂等性
    root.addHandler(handler)
    root.setLevel(level.upper())  # 设置日志等级


@contextmanager
def session_log_context(session_id: str):
    """把一段代码的日志绑定到指定会话。

    Args:
        session_id: 会话ID。

    Yields:
        None: 上下文内产生的日志均带上该会话ID。
    """
    token = session_id_var.set(session_id)  # 当前协程上下文设置为该会话ID
    try:
        yield
    finally:
        session_id_var.reset(token)


def bind_session_id(session_id: str):
    """设置当前协程上下文的会话ID, 返回token。

    Args:
        session_id: 会话ID。

    Returns:
        Token: 用于恢复上下文的token令牌, 需要传给 unbind_session_id 函数。
    """
    return session_id_var.set(session_id)


def unbind_session_id(token) -> None:
    """恢复会话ID上下文。

    Args:
        token: 上下文令牌, 由 bind_session_id 函数返回。
    """
    session_id_var.reset(token)
