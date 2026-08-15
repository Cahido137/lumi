from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.config import get_dbsettings


# 获取数据库配置信息
db_settings = get_dbsettings()

# 创建异步引擎
async_engine = create_async_engine(
    url=db_settings.database_url,
    echo=True,
    pool_pre_ping=True
)

# 拿到会话类
SessionLocal = async_sessionmaker(
    bind=async_engine,
    expire_on_commit=False
)