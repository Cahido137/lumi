from logging.config import fileConfig

from alembic import context
from app.config import get_dbsettings
from app.db import models  # noqa: F401 导入即把全部模型注册到 Base.metadata
from app.db.base import Base
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    # 仅配置 alembic 命令自身的日志输出, 与应用日志互不影响
    fileConfig(config.config_file_name)

# 优先用 -x db_url=... 传入的连接串(对空库生成baseline时用), 否则用 .env 的 DATABASE_URL。
# 迁移用同步驱动(psycopg), 所以 +asyncpg 换成 +psycopg;
# % 转义为 %% 防止 configparser 插值报错
x_args = context.get_x_argument(as_dictionary=True)
url = x_args.get("db_url") or get_dbsettings().database_url
config.set_main_option("sqlalchemy.url", url.replace("+asyncpg", "+psycopg").replace("%", "%%"))

target_metadata = Base.metadata

# langgraph检查点表由 setup_checkpoint() 自行管理, 不参与迁移对比,
# 否则 autogenerate/check 会把它们误判成"多余的库表"而生成删除操作
LANGGRAPH_TABLES = {"checkpoints", "checkpoint_writes", "checkpoint_blobs", "checkpoint_migrations"}


def include_object(object, name, type_, reflected, compare_to):
    """排除langgraph的表, 其余对象全部参与对比"""
    if type_ == "table" and name in LANGGRAPH_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    """离线模式: 只生成SQL不连库(alembic upgrade --sql)"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式: 连接数据库执行迁移(默认)"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
