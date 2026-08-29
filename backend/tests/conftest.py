"""全局测试夹具: 测试库隔离与数据清理"""
import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

# Windows下切换SelectorEventLoop: psycopg异步连接不能跑在ProactorEventLoop上,
# 与生产入口(run.py)保持一致, 必须在pytest创建事件循环之前执行
from app.core import compat  # noqa: F401

# 必须在 import 任何 app 模块之前设置, 让引擎/检查点/迁移全部指向测试库
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://agent:agent123@127.0.0.1:5432/agent_test"
)

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_URL_SYNC = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg")


@pytest.fixture(scope="session")
def test_db():
    """建测试库(不存在才建)并迁移到最新结构, 整个会话只跑一次"""
    # psycopg 直连用 libpq 格式, 不能带 +psycopg 后缀
    admin_url = os.environ["DATABASE_URL"].replace("+asyncpg", "").rsplit("/", 1)[0] + "/postgres"
    conn = psycopg.connect(admin_url, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = 'agent_test'")
        if cur.fetchone() is None:
            cur.execute("CREATE DATABASE agent_test")
    conn.close()

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(scope="session")
async def checkpoint(test_db):
    """初始化测试库里的 langgraph 检查点表, 会话结束关闭连接池"""
    from app.core.checkpoint import setup_checkpoint, close_checkpoint

    await setup_checkpoint()
    yield
    await close_checkpoint()


@pytest.fixture()
async def clean_db(test_db, checkpoint):
    """每个测试前清空业务表与进程内全局状态, 保证测试间完全隔离"""
    from sqlalchemy import text

    from app.core.event_bus import event_bus
    import app.core.session_runner.state as state
    from app.db.session import async_engine

    # RESTART IDENTITY 重置自增列, 每个测试的 uid 都从10000开始
    async with async_engine.begin() as conn:
        await conn.execute(text(
            "TRUNCATE approvals, tool_executions, todos, messages, sessions, users "
            "RESTART IDENTITY CASCADE"
        ))
    # langgraph 的 checkpoint 表不清理, 由 langgraph 自行管理
    state._session_lock.clear()
    state._cancel_events.clear()
    state._cancel_generations.clear()
    state._active_runs.clear()
    state._active_tasks.clear()
    state._pending_runs.clear()
    event_bus._queues.clear()
    yield
