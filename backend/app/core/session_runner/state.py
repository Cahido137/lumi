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