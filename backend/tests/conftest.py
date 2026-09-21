"""全局测试夹具: 测试库隔离与数据清理"""

import os
from argparse import Namespace
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

# Windows下切换SelectorEventLoop: psycopg异步连接不能跑在ProactorEventLoop上,
# 与生产入口(run.py)保持一致, 必须在pytest创建事件循环之前执行
from app.core import compat  # noqa: F401
from psycopg import sql
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

TEST_DATABASE_NAME = "agent_test"
"""允许迁移与清理的测试数据库名。"""


def _load_test_url() -> URL:
    """解析并校验测试数据库连接地址。

    Returns:
        URL: 通过白名单校验的连接地址。

    Raises:
        pytest.UsageError: 地址不合法或目标不在白名单内。
    """
    try:
        url = make_url(
            os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://agent:agent123@127.0.0.1:5432/agent_test")
        )
    except (ArgumentError, ValueError):
        raise pytest.UsageError("TEST_DATABASE_URL 格式不合法") from None
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"127.0.0.1", "localhost"}
        or url.port not in {None, 5432}
        or url.database != TEST_DATABASE_NAME
        or url.query
    ):
        raise pytest.UsageError("测试仅允许连接本机 5432 上的 agent_test, URL 不得带查询参数")
    if os.environ.get("PGHOSTADDR") or os.environ.get("PGSERVICE"):
        raise pytest.UsageError("测试不允许设置 PGHOSTADDR 或 PGSERVICE")
    return url.set(port=5432)


def _require_test_database(database: str | None) -> None:
    """校验连接实际访问的数据库。

    Args:
        database: current_database() 返回的数据库名。

    Raises:
        pytest.UsageError: 当前连接不属于允许的测试库。
    """
    if database != TEST_DATABASE_NAME:
        raise pytest.UsageError("当前连接不是 agent_test, 已拒绝迁移或清理")


TEST_URL = _load_test_url()
"""通过校验的测试连接配置。"""

# 必须在导入数据库配置、引擎与检查点前固定目标
os.environ["DATABASE_URL"] = TEST_URL.render_as_string(hide_password=False)
BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def test_db():
    """建测试库并校验实际目标, 然后迁移到最新结构。"""
    sync_url = TEST_URL.set(drivername="postgresql")
    admin_url = sync_url.set(database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE_NAME,)).fetchone() is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DATABASE_NAME)))

    with psycopg.connect(sync_url.render_as_string(hide_password=False), connect_timeout=5) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
        _require_test_database(row[0] if row is not None else None)

    cfg = Config(
        str(BACKEND_DIR / "alembic.ini"),
        cmd_opts=Namespace(x=[f"db_url={TEST_URL.render_as_string(hide_password=False)}"]),
    )
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(scope="session")
async def checkpoint(test_db):
    """初始化测试库里的 langgraph 检查点表, 会话结束关闭连接池"""
    from app.core.checkpoint import close_checkpoint, setup_checkpoint

    await setup_checkpoint()
    yield
    await close_checkpoint()


@pytest.fixture()
async def clean_db(test_db, checkpoint):
    """每个测试前清空业务表与进程内全局状态, 保证测试间完全隔离"""
    import app.core.session_runner.state as state
    from app.core.event_bus import event_bus
    from app.db.session import async_engine
    from sqlalchemy import text

    # RESTART IDENTITY 重置自增列, 每个测试的 uid 都从10000开始
    async with async_engine.begin() as conn:
        database = (await conn.execute(text("SELECT current_database()"))).scalar_one()
        _require_test_database(database)
        await conn.execute(
            text("TRUNCATE runs, approvals, tool_executions, todos, messages, sessions, users RESTART IDENTITY CASCADE")
        )
    # langgraph 的 checkpoint 表不清理, 由 langgraph 自行管理
    state._session_lock.clear()
    state._cancel_events.clear()
    state._cancel_generations.clear()
    state._active_runs.clear()
    state._active_tasks.clear()
    state._pending_runs.clear()
    event_bus._queues.clear()
    yield
