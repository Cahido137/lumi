"""测试库保护的离线回归验证。"""

import os
import runpy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import psycopg
import pytest
from alembic import command
from sqlalchemy.engine import make_url

CONFTEST_PATH = Path(__file__).resolve().parents[1] / "conftest.py"
"""需要验证的根测试夹具文件。"""

TEST_URL = "postgresql+asyncpg://agent:fake-secret@127.0.0.1:5432/agent_test"
"""仅供离线测试使用的连接地址。"""


@pytest.fixture
def load_test_environment(monkeypatch):
    """隔离环境变量并禁止测试保护用例建立真实连接。"""
    monkeypatch.setenv("DATABASE_URL", os.environ["DATABASE_URL"])
    for name in ("TEST_DATABASE_URL", "PGHOSTADDR", "PGSERVICE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(psycopg, "connect", Mock(side_effect=AssertionError("不允许连接真实数据库")))
    monkeypatch.setattr(command, "upgrade", Mock(side_effect=AssertionError("不允许执行真实迁移")))

    def load(url=None):
        if url is not None:
            monkeypatch.setenv("TEST_DATABASE_URL", url)
        return runpy.run_path(str(CONFTEST_PATH))

    return load


@pytest.mark.parametrize(
    "url",
    [
        TEST_URL.replace("/agent_test", "/agent_db"),
        TEST_URL.replace("/agent_test", "/agent_test_copy"),
        TEST_URL.replace("127.0.0.1", "remote"),
        TEST_URL.replace(":5432/", ":5433/"),
        TEST_URL + "?dbname=agent_db",
        TEST_URL.replace("+asyncpg", ""),
        "malformed-fake-secret",
        TEST_URL.replace(":5432/", ":invalid/"),
    ],
    ids=["business", "similar-name", "host", "port", "query", "driver", "malformed", "invalid-port"],
)
def test_unsafe_url_is_rejected_before_connecting(load_test_environment, url):
    """错误目标在加载夹具时失败, 不连接、不迁移、不回显密码。"""
    with pytest.raises(pytest.UsageError) as caught:
        load_test_environment(url)
    assert "fake-secret" not in str(caught.value)
    psycopg.connect.assert_not_called()
    command.upgrade.assert_not_called()


@pytest.mark.parametrize("name", ["PGHOSTADDR", "PGSERVICE"])
def test_libpq_redirect_is_rejected(load_test_environment, monkeypatch, name):
    """连接重定向环境变量不能绕过 URL 白名单。"""
    monkeypatch.setenv(name, "other-target")
    with pytest.raises(pytest.UsageError):
        load_test_environment(TEST_URL)
    psycopg.connect.assert_not_called()


@pytest.mark.parametrize("host_port", ["127.0.0.1:5432", "localhost:5432", "127.0.0.1"])
def test_allowed_url_is_normalized(load_test_environment, host_port):
    """允许本地测试库, 省略端口时补为 5432。"""
    url = TEST_URL.replace("127.0.0.1:5432", host_port)
    load_test_environment(url)
    assert make_url(os.environ["DATABASE_URL"]) == make_url(url).set(port=5432)
    psycopg.connect.assert_not_called()


def test_default_target_is_agent_test(load_test_environment):
    """不设置 TEST_DATABASE_URL 时保留原有默认测试库。"""
    load_test_environment()
    url = make_url(os.environ["DATABASE_URL"])
    assert (url.host, url.port, url.database) == ("127.0.0.1", 5432, "agent_test")


def mock_sync_database(monkeypatch, database):
    """替换建库与身份查询连接, 返回可观察的迁移替身。"""
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.side_effect = [(1,), None if database is None else (database,)]
    monkeypatch.setattr(psycopg, "connect", Mock(return_value=connection))
    upgrade = Mock()
    monkeypatch.setattr(command, "upgrade", upgrade)
    return upgrade


@pytest.mark.parametrize("database", ["agent_db", None])
def test_actual_database_is_checked_before_migration(load_test_environment, monkeypatch, database):
    """连接实际误指业务库或无法确认身份时, 不执行迁移。"""
    fixtures = load_test_environment(TEST_URL)
    upgrade = mock_sync_database(monkeypatch, database)
    fixture = fixtures["test_db"].__wrapped__()
    try:
        with pytest.raises(pytest.UsageError):
            next(fixture)
    finally:
        fixture.close()
    upgrade.assert_not_called()


def test_migration_uses_verified_url(load_test_environment, monkeypatch):
    """迁移显式使用已校验地址, 不重新读取后来改变的环境变量。"""
    fixtures = load_test_environment(TEST_URL)
    upgrade = mock_sync_database(monkeypatch, "agent_test")
    monkeypatch.setenv("DATABASE_URL", TEST_URL.replace("/agent_test", "/agent_db"))
    fixture = fixtures["test_db"].__wrapped__()
    try:
        next(fixture)
    finally:
        fixture.close()
    upgrade.assert_called_once()
    cfg, revision = upgrade.call_args.args
    assert revision == "head"
    assert cfg.cmd_opts.x == [f"db_url={TEST_URL}"]


@pytest.mark.parametrize("database", ["agent_db", "agent_test_copy", "agent_test"])
async def test_cleanup_checks_actual_database_before_truncate(load_test_environment, monkeypatch, database):
    """同一清理连接先核对库名, 只有测试库允许继续清理。"""
    import app.core.session_runner.state as state
    import app.db.session as session
    from app.core.event_bus import event_bus

    fixtures = load_test_environment(TEST_URL)
    for name in (
        "_session_lock",
        "_cancel_events",
        "_cancel_generations",
        "_active_runs",
        "_active_tasks",
        "_pending_runs",
    ):
        monkeypatch.setattr(state, name, type(getattr(state, name))())
    monkeypatch.setattr(event_bus, "_queues", {})
    result = Mock()
    result.scalar_one.return_value = database
    connection = Mock()
    connection.execute = AsyncMock(return_value=result)
    if database != "agent_test":
        connection.execute.side_effect = [result, AssertionError("禁止继续执行清理语句")]
    engine = MagicMock()
    engine.begin.return_value.__aenter__.return_value = connection
    monkeypatch.setattr(session, "async_engine", engine)
    fixture = fixtures["clean_db"].__wrapped__(None, None)
    try:
        if database == "agent_test":
            await anext(fixture)
        else:
            with pytest.raises(pytest.UsageError):
                await anext(fixture)
    finally:
        await fixture.aclose()
    statements = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert statements[0] == "SELECT current_database()"
    if database == "agent_test":
        assert len(statements) == 2
        assert statements[1].startswith("TRUNCATE ")
    else:
        assert len(statements) == 1
