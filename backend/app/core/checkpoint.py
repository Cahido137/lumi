"""检查点管理"""

from langgraph.checkpoint.postgres.aio import AsyncConnectionPool, AsyncPostgresSaver

from app.config import get_dbsettings

# 全局连接池
_pool: AsyncConnectionPool | None = None


def _create_pool() -> AsyncConnectionPool:
    # 建立数据库连接url
    db_url = get_dbsettings().database_url.replace("+asyncpg", "") + "?sslmode=disable"
    # 初始化连接池
    pool = AsyncConnectionPool(
        conninfo=db_url, max_size=10, kwargs={"autocommit": True, "prepare_threshold": 0}, open=False
    )
    return pool


def get_checkpointer() -> AsyncPostgresSaver:
    """获取异步检查点"""
    global _pool
    if _pool is None:
        _pool = _create_pool()
    return AsyncPostgresSaver(_pool)


async def setup_checkpoint() -> None:
    """初始化检查点"""
    global _pool
    if _pool is None:
        _pool = _create_pool()
        await _pool.open()  # 开启连接池
    await get_checkpointer().setup()  # 初始化表


async def close_checkpoint() -> None:
    """关闭连接池"""
    global _pool
    if _pool is not None:
        await _pool.close()  # 关闭连接池
        _pool = None
