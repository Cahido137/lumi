from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.config import get_dbsettings, get_logsettings


# 获取数据库配置信息
db_settings = get_dbsettings()
# 获取日志配置信息
log_settings = get_logsettings()

# 创建异步引擎
async_engine = create_async_engine(
    url=db_settings.database_url,
    echo=log_settings.database_echo,
    pool_pre_ping=True
)

# 拿到会话类
SessionLocal = async_sessionmaker(
    bind=async_engine,
    expire_on_commit=False
)


async def get_db():
    """获取数据库会话依赖"""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()