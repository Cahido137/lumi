"""检查点管理。

Note:
    本模块负责维护一个独立于 SQLAlchemy 引擎的 psycopg 连接池, 两者连接同一数据库。
"""

from langgraph.checkpoint.postgres.aio import AsyncConnectionPool, AsyncPostgresSaver

from app.config import get_dbsettings

_pool: AsyncConnectionPool | None = None
"""检查点专用的 psycopg 异步连接池, 首次连接时惰性创建。"""


def _create_pool() -> AsyncConnectionPool:
    """创建一个关闭状态的检查点连接池。

    Returns:
        AsyncConnectionPool: 处于关闭状态的连接池。
    """
    base = get_dbsettings().database_url.replace("+asyncpg", "")
    sep = "&" if "?" in base else "?"
    db_url = f"{base}{sep}sslmode=disable"
    # 初始化连接池
    pool = AsyncConnectionPool(
        conninfo=db_url, max_size=10, kwargs={"autocommit": True, "prepare_threshold": 0}, open=False
    )
    return pool


def get_checkpointer() -> AsyncPostgresSaver:
    """获取异步检查点保存器。

    Returns:
        AsyncPostgresSaver: 绑定在进程内连接池上的检查点保存器。

    Note:
        本函数只在连接池不存在时创建连接池, 不负责打开。
    """
    global _pool
    if _pool is None:
        _pool = _create_pool()
    return AsyncPostgresSaver(_pool)  # type: ignore[arg-type]


async def setup_checkpoint() -> None:
    """打开连接池并初始化检查点所需的表结构。"""
    global _pool
    if _pool is None:
        _pool = _create_pool()
        await _pool.open()  # 开启连接池
    await get_checkpointer().setup()  # 初始化表


async def close_checkpoint() -> None:
    """关闭连接池。"""
    global _pool
    if _pool is not None:
        await _pool.close()  # 关闭连接池
        _pool = None


async def ping_checkpoint() -> tuple[bool, str]:
    """检查检查点连接池是否可用。

    Returns:
        tuple[bool, str]: (是否可用, 详细信息)。
    """
    if _pool is None:
        return False, "连接池尚未初始化"
    if _pool.closed:
        return False, "连接池已关闭"
    try:
        async with _pool.connection() as conn:
            await conn.execute("SELECT 1")
    except Exception as e:
        return False, f"连接池探测失败: {type(e).__name__}"
    return True, "连接池可用"
