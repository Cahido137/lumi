"""会话运行共享状态"""

import asyncio


# 会话级运行锁
_session_lock: dict[str, asyncio.Lock] = {}  # {会话ID: 运行锁对象}

def get_session_lock(session_id: str) -> asyncio.Lock:
    """获取会话级运行锁对象"""
    lock = _session_lock.get(session_id)
    # 如果还没有锁就创建一个锁
    if lock is None:
        lock = asyncio.Lock()
        _session_lock[session_id] = lock
    return lock


# 会话级取消事件
_cancel_events: dict[str, asyncio.Event] = {}  # {会话ID: 取消事件对象}

def get_cancel_event(session_id: str) -> asyncio.Event:
    """获取会话级取消事件对象"""
    event = _cancel_events.get(session_id)
    if event is None:
        event = asyncio.Event()
        _cancel_events[session_id] = event
    return event


# 会话取消代际
_cancel_generations: dict[str, int] = {}  # {会话ID: 当前代际}
def get_cancel_generation(session_id: str) -> int:
    """获取会话当前的取消代际"""
    return _cancel_generations.get(session_id, 0)

def bump_cancel_generation(session_id: str) -> None:
    """递增指定会话的代际"""
    _cancel_generations[session_id] = get_cancel_generation(session_id) + 1


_active_runs: set[str] = set()  # 正在运行的会话ID集合
_active_tasks: dict[str, asyncio.Task] = {}  # {会话ID: 图流生产者任务}

def register_active_task(session_id: str, task: asyncio.Task) -> None:
    """给指定会话登记图流生产者任务"""
    _active_tasks[session_id] = task

def unregister_active_task(session_id: str) -> None:
    """注销指定会话的图流生产者任务"""
    _active_tasks.pop(session_id, None)


# 会话中未完成轮次计数
_pending_runs: dict[str, int] = {}  # {会话ID: 未完成轮次数}

def register_pending_run(session_id: str) -> None:
    """登记一轮未完成的运行"""
    _pending_runs[session_id] = _pending_runs.get(session_id, 0) + 1

def unregister_pending_run(session_id: str) -> None:
    """注销一轮运行"""
    count = _pending_runs.get(session_id, 0)
    if count <= 1:
        _pending_runs.pop(session_id, None)
    else:
        _pending_runs[session_id] = count - 1

def has_pending_runs(session_id: str) -> bool:
    """是否存在排队中或执行中的轮次"""
    return _pending_runs.get(session_id, 0) > 0


# 中断哨兵
_CANCELLED = object()

# 中断插入消息文案
CANCEL_MESSAGE = "[对话已被用户打断]"


class RunCancelledError(Exception):
    """运行时被打断异常"""
    def __init__(self, message: str = CANCEL_MESSAGE, streamed_text: str = ""):
        """
        Args:
            message: 中断提示消息
            streamed_text: 中断前已经流式发出的模型回复文本
        """
        super().__init__(message)
        self.message = message
        self.streamed_text = streamed_text


def request_cancel_session(session_id: str) -> bool:
    """请求中断会话运行"""
    running = session_id in _active_runs  # 判断是否是正在运行的会话
    if running:
        task = _active_tasks.get(session_id)
        # 如果确实存在正在运行的任务
        if task is not None and not task.done():
            task.cancel()  # 立即取消任务
    get_cancel_event(session_id).set()
    # 代际加一，作废还在排队的任务
    bump_cancel_generation(session_id)
    return running or has_pending_runs(session_id)