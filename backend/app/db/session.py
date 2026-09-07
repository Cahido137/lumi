"""异步数据库引擎、会话工厂与依赖。"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_dbsettings, get_logsettings

db_settings = get_dbsettings()
"""数据库配置单例, 提供异步引擎连接所需的 URL。"""

log_settings = get_logsettings()
"""日志配置单例, 提供 SQLAlchemy 的 SQL 回显开关, 可用于调试。"""

async_engine = create_async_engine(url=db_settings.database_url, echo=log_settings.database_echo, pool_pre_ping=True)
"""全局异步引擎, 进程内唯一, 持有数据库连接池。"""

SessionLocal = async_sessionmaker(bind=async_engine, expire_on_commit=False)
"""异步会话工厂, 调用一次得到一个独立的数据库会话。

expire_on_commit 设置为 False 表示提交后不将已加载对象标记为过期,
调用方在 commit 之后仍可以直接读取对象属性。
若标记为 True, 提交后的任何属性访问都会触发一次隐式查询操作。
"""


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """提供请求级数据库会话的 FastAPI 依赖, 用于 Depends 注入。

    使用异步生成器实现, yield 处挂起并把会话交给路由函数。

    Yields:
        AsyncSession: 绑定在当前请求上的数据库会话。

    Note:
        本依赖会统一进行数据库事务提交操作, 因此在路由处理函数中无需再次手动 commit。
        当出现异常时会自动进行回滚并向上抛出异常, 因此应由上层函数处理异常。
    """
    # 从连接池取一个会话连接
    async with SessionLocal() as session:
        try:
            # 把会话交给调用方路由函数，从此处函数挂起
            yield session
            await session.commit()
        except Exception:
            # 处理函数异常，回滚整个事务，以保持一致性
            await session.rollback()
            raise  # 向上抛出异常
        finally:
            # 关闭连接释放资源
            await session.close()
