"""深度体检执行、单飞与缓存的单元测试(离线, 探针与会话工厂全部替换)。"""

import asyncio
import time

import pytest
from app.config import get_healthsettings
from app.core import diagnostics
from app.core.diagnostics import (
    DeepCheckGuard,
    deep_check,
    default_deep_check_guard,
    probe_database,
    run_deep_checks,
    summarize,
)
from app.core.execution.env import own_config_keys, scrubbed_env
from app.schemas.health import CheckItem, DeepChecks
from sqlalchemy.exc import OperationalError

SECRET_BASE_URL = "https://llm.internal.example:8443/v1"
"""伪供应商地址, 用于断言它不会出现在对外响应中。"""

SECRET_PROVIDER_DETAIL = f"模型提供商未提供模型列表接口: {SECRET_BASE_URL}/models"
"""模拟 ping_provider 失败时的真实文案形态。"""

SECRET_MODEL_DETAIL = "Disconnected. Details: \nAuthenticationError: invalid key sk-leak"
"""模拟 ping_chat_model 失败时的真实文案形态。"""

HEALTH_ENV_KEYS = (
    "HEALTH_DEEP_ENABLED",
    "HEALTH_DEEP_CACHE_TTL",
    "HEALTH_DEEP_FAILURE_TTL",
    "HEALTH_DEEP_MODEL_TIMEOUT",
)
"""本层新增的配置项环境变量名。"""

CHECK_NAMES = ("database", "checkpoint", "provider", "llm_generation")
"""深度体检的四个检查项名称。"""


def make_probe(ok: bool = True, raw: str = "probe-raw", delay: float = 0.0, calls: list | None = None):
    """构造一个可计数、可延时的假探针。

    Args:
        ok: 探针返回值的第一项。
        raw: 探针返回值的第二项, 即不应对外回显的原始详情。
        delay: 探针执行前的等待秒数。
        calls: 用于记录调用时刻的列表。

    Returns:
        Probe: 可直接注入 run_deep_checks 的假探针。
    """

    async def _probe() -> tuple[bool, str]:
        if calls is not None:
            calls.append(time.monotonic())
        if delay > 0:
            await asyncio.sleep(delay)
        return ok, raw

    return _probe


def make_checks(failed: str | None = None) -> DeepChecks:
    """构造四项检查结果。

    Args:
        failed: 需要标记为不通过的检查项名称, 为空时四项全通过。

    Returns:
        DeepChecks: 构造好的检查结果。
    """
    items = {name: CheckItem(ok=name != failed, detail=f"{name} detail", latency_ms=1.0) for name in CHECK_NAMES}
    return DeepChecks(**items)


def make_guard(checks: DeepChecks, *, cache_ttl: float = 60.0, failure_ttl: float = 5.0, delay: float = 0.0, runs=None):
    """构造一个使用假执行函数的闸门。

    Args:
        checks: 每次真实执行返回的结果。
        cache_ttl: 通过结果的复用秒数。
        failure_ttl: 降级结果的复用秒数。
        delay: 单次真实执行的等待秒数, 用于制造并发窗口。
        runs: 用于记录真实执行时刻的列表。

    Returns:
        DeepCheckGuard: 构造好的闸门。
    """

    async def runner() -> DeepChecks:
        if runs is not None:
            runs.append(time.monotonic())
        if delay > 0:
            await asyncio.sleep(delay)
        return checks

    return DeepCheckGuard(cache_ttl=cache_ttl, failure_ttl=failure_ttl, runner=runner)


# --------------------------------------------------------------------------- #
# 探针执行与详情收敛
# --------------------------------------------------------------------------- #


async def test_run_deep_checks_all_healthy() -> None:
    """四项全通过时逐项 ok, 汇总为 healthy, detail 为固定文案。"""
    checks = await run_deep_checks(
        model_timeout=1.0,
        database_probe=make_probe(raw="SELECT 1 succeeded"),
        checkpoint_probe=make_probe(raw="连接池可用"),
        provider_probe=make_probe(raw="供应商可达"),
        model_probe=make_probe(raw="Connected. LLM return: ping"),
    )
    assert all(getattr(checks, name).ok for name in CHECK_NAMES)
    assert summarize(checks) == "healthy"
    assert checks.database.detail == "Database connected."
    assert checks.checkpoint.detail == "Checkpoint available."
    assert checks.provider.detail == "Provider reachable."
    assert checks.llm_generation.detail == "Model generation succeeded."
    assert all(getattr(checks, name).latency_ms is not None for name in CHECK_NAMES)


async def test_run_deep_checks_never_echoes_probe_detail() -> None:
    """探针原文(供应商地址、密钥、异常文本)一律不出现在对外 detail 中。"""
    checks = await run_deep_checks(
        model_timeout=1.0,
        database_probe=make_probe(ok=False, raw="OperationalError: connection refused to db.internal:5432"),
        checkpoint_probe=make_probe(ok=False, raw="连接池探测失败: OperationalError"),
        provider_probe=make_probe(raw=SECRET_PROVIDER_DETAIL),
        model_probe=make_probe(ok=False, raw=SECRET_MODEL_DETAIL),
    )
    dumped = repr(checks.model_dump())
    for secret in (SECRET_BASE_URL, "sk-leak", "db.internal:5432", "AuthenticationError", "连接池探测失败"):
        assert secret not in dumped
    assert checks.database.detail == "Database unreachable."
    assert checks.checkpoint.detail == "Checkpoint unavailable."
    assert checks.llm_generation.detail == "Model generation failed."
    assert summarize(checks) == "degraded"


async def test_run_deep_checks_skips_model_when_provider_unreachable() -> None:
    """供应商不可达时不再发起模型调用, 也不再花一次费用。"""
    calls: list = []
    checks = await run_deep_checks(
        model_timeout=1.0,
        database_probe=make_probe(),
        checkpoint_probe=make_probe(),
        provider_probe=make_probe(ok=False, raw="无法连接至供应商"),
        model_probe=make_probe(calls=calls),
    )
    assert calls == []
    assert checks.llm_generation.ok is False
    assert checks.llm_generation.detail == "Skipped because the provider is unreachable."


async def test_run_deep_checks_measures_per_item_latency() -> None:
    """四项分别计时, 慢的那项耗时更长。"""
    checks = await run_deep_checks(
        model_timeout=1.0,
        database_probe=make_probe(),
        checkpoint_probe=make_probe(),
        provider_probe=make_probe(delay=0.02),
        model_probe=make_probe(),
    )
    assert checks.provider.latency_ms >= 15.0
    assert checks.database.latency_ms < checks.provider.latency_ms


async def test_run_deep_checks_measures_model_latency_separately() -> None:
    """模型生成项单独计时, 不把供应商探针的耗时算进来。"""
    checks = await run_deep_checks(
        model_timeout=1.0,
        database_probe=make_probe(),
        checkpoint_probe=make_probe(),
        provider_probe=make_probe(delay=0.03),
        model_probe=make_probe(),
    )
    assert checks.provider.latency_ms >= 25.0
    assert checks.llm_generation.latency_ms < 15.0


async def test_run_deep_checks_resolves_default_probes_late(monkeypatch: pytest.MonkeyPatch) -> None:
    """不注入探针时也在调用点解析模块属性, 顺序为库→检查点→供应商→模型。"""
    calls: list[str] = []

    def named(name: str):
        async def _probe() -> tuple[bool, str]:
            calls.append(name)
            return True, "ok"

        return _probe

    async def fake_model(llm=None, timeout: float = 15.0) -> tuple[bool, str]:
        calls.append(f"model:{timeout}")
        return True, "ok"

    monkeypatch.setattr(diagnostics, "probe_database", named("database"))
    monkeypatch.setattr(diagnostics, "ping_checkpoint", named("checkpoint"))
    monkeypatch.setattr(diagnostics, "ping_provider", named("provider"))
    monkeypatch.setattr(diagnostics, "ping_chat_model", fake_model)
    checks = await run_deep_checks(model_timeout=3.0)
    assert calls == ["database", "checkpoint", "provider", "model:3.0"]
    assert summarize(checks) == "healthy"


@pytest.mark.parametrize("failed", list(CHECK_NAMES))
def test_summarize_degraded_on_any_single_failure(failed: str) -> None:
    """任意一项不通过即汇总为 degraded。"""
    assert summarize(make_checks(failed=failed)) == "degraded"
    assert summarize(make_checks()) == "healthy"


# --------------------------------------------------------------------------- #
# 数据库探针
# --------------------------------------------------------------------------- #


class FakeSession:
    """记录调用顺序的假数据库会话。"""

    def __init__(self, log: list[str], error: Exception | None = None) -> None:
        self._log = log
        self._error = error

    async def __aenter__(self) -> "FakeSession":
        self._log.append("enter")
        return self

    async def __aexit__(self, *exc_info) -> bool:
        self._log.append("exit")
        return False

    async def execute(self, statement) -> None:
        """执行语句, 需要时抛出预置异常。"""
        self._log.append("execute")
        if self._error is not None:
            raise self._error


async def test_probe_database_closes_session_before_returning(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测结束即归还连接, 不把事务带进后续耗时探针。"""
    log: list[str] = []
    monkeypatch.setattr(diagnostics, "SessionLocal", lambda: FakeSession(log))
    ok, raw = await probe_database()
    assert (ok, raw) == (True, "SELECT 1 succeeded")
    assert log == ["enter", "execute", "exit"]


async def test_probe_database_reports_failure_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """数据库异常收敛为不通过, 原始异常留在详情里(详情只写日志)。"""
    log: list[str] = []
    error = OperationalError("SELECT 1", None, RuntimeError("connection refused"))
    monkeypatch.setattr(diagnostics, "SessionLocal", lambda: FakeSession(log, error))
    ok, raw = await probe_database()
    assert ok is False
    assert "OperationalError" in raw
    assert log == ["enter", "execute", "exit"]


# --------------------------------------------------------------------------- #
# 单飞与短期缓存
# --------------------------------------------------------------------------- #


async def test_guard_runs_once_within_ttl() -> None:
    """TTL 内重复调用只真实执行一次, 之后的响应标记为缓存。"""
    runs: list = []
    guard = make_guard(make_checks(), cache_ttl=60.0, runs=runs)
    first = await guard.get()
    second = await guard.get()
    third = await guard.get()
    assert len(runs) == 1
    assert (first.cached, second.cached, third.cached) == (False, True, True)
    assert third.age_ms >= second.age_ms >= 0.0
    assert first.status == second.status == "healthy"


async def test_guard_reruns_after_cache_ttl_expires() -> None:
    """TTL 过期后重新真实执行。"""
    runs: list = []
    guard = make_guard(make_checks(), cache_ttl=0.01, runs=runs)
    await guard.get()
    await asyncio.sleep(0.03)
    result = await guard.get()
    assert len(runs) == 2
    assert result.cached is False


async def test_guard_uses_failure_ttl_for_degraded_result() -> None:
    """降级结果用更短的 TTL, 故障恢复不会被长时间掩盖。"""
    runs: list = []
    guard = make_guard(make_checks(failed="provider"), cache_ttl=60.0, failure_ttl=0.01, runs=runs)
    first = await guard.get()
    await asyncio.sleep(0.03)
    await guard.get()
    assert first.status == "degraded"
    assert len(runs) == 2


async def test_guard_keeps_healthy_result_beyond_failure_ttl() -> None:
    """通过结果的复用窗口是 cache_ttl, 不受 failure_ttl 影响。"""
    runs: list = []
    guard = make_guard(make_checks(), cache_ttl=60.0, failure_ttl=0.01, runs=runs)
    await guard.get()
    await asyncio.sleep(0.03)
    result = await guard.get()
    assert len(runs) == 1
    assert result.cached is True


async def test_guard_coalesces_concurrent_callers() -> None:
    """并发调用被合并成一次真实执行, 其余复用同一份结果。"""
    runs: list = []
    guard = make_guard(make_checks(), delay=0.05, runs=runs)
    results = await asyncio.gather(*(guard.get() for _ in range(8)))
    assert len(runs) == 1
    assert [item.cached for item in results].count(False) == 1
    assert all(item.status == "healthy" for item in results)


async def test_guard_passes_checks_and_status_through() -> None:
    """响应体透传逐项结果与汇总状态。"""
    checks = make_checks(failed="checkpoint")
    result = await make_guard(checks).get()
    assert result.status == "degraded"
    assert result.checks == checks
    assert result.checks.checkpoint.ok is False


async def test_deep_check_uses_injected_guard() -> None:
    """显式传入闸门时不触碰进程级默认闸门。"""
    runs: list = []
    result = await deep_check(guard=make_guard(make_checks(), runs=runs))
    assert len(runs) == 1
    assert result.cached is False


def test_default_guard_is_cached_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认闸门按进程缓存, 清缓存后重建。"""
    for name in HEALTH_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    get_healthsettings.cache_clear()
    default_deep_check_guard.cache_clear()
    try:
        first = default_deep_check_guard()
        assert default_deep_check_guard() is first
        default_deep_check_guard.cache_clear()
        assert default_deep_check_guard() is not first
    finally:
        get_healthsettings.cache_clear()
        default_deep_check_guard.cache_clear()


# --------------------------------------------------------------------------- #
# 配置装配
# --------------------------------------------------------------------------- #


def test_health_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置时端点开放, 通过结果复用 60s, 降级结果复用 5s。"""
    for name in HEALTH_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    get_healthsettings.cache_clear()
    try:
        settings = get_healthsettings()
        assert settings.health_deep_enabled is True
        assert (settings.health_deep_cache_ttl, settings.health_deep_failure_ttl) == (60.0, 5.0)
        assert settings.health_deep_model_timeout == 5.0
        # 降级结果的复用窗口必须短于通过结果, 否则故障恢复会被掩盖
        assert settings.health_deep_failure_ttl < settings.health_deep_cache_ttl
    finally:
        get_healthsettings.cache_clear()


def test_health_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """四个环境变量都能装配进配置。"""
    monkeypatch.setenv("HEALTH_DEEP_ENABLED", "false")
    monkeypatch.setenv("HEALTH_DEEP_CACHE_TTL", "12.5")
    monkeypatch.setenv("HEALTH_DEEP_FAILURE_TTL", "1.5")
    monkeypatch.setenv("HEALTH_DEEP_MODEL_TIMEOUT", "2.5")
    get_healthsettings.cache_clear()
    try:
        settings = get_healthsettings()
        assert settings.health_deep_enabled is False
        assert (settings.health_deep_cache_ttl, settings.health_deep_failure_ttl) == (12.5, 1.5)
        assert settings.health_deep_model_timeout == 2.5
    finally:
        get_healthsettings.cache_clear()


def test_default_guard_takes_ttl_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认闸门的两个 TTL 来自配置。"""
    monkeypatch.setenv("HEALTH_DEEP_CACHE_TTL", "7.0")
    monkeypatch.setenv("HEALTH_DEEP_FAILURE_TTL", "0.5")
    get_healthsettings.cache_clear()
    default_deep_check_guard.cache_clear()
    try:
        guard = default_deep_check_guard()
        assert (guard.cache_ttl, guard.failure_ttl) == (7.0, 0.5)
    finally:
        get_healthsettings.cache_clear()
        default_deep_check_guard.cache_clear()


def test_health_keys_are_scrubbed_from_subprocess_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """健康检查配置项不会透传给工具子进程。"""
    for name in HEALTH_ENV_KEYS:
        assert name in own_config_keys()
    monkeypatch.setenv("HEALTH_DEEP_ENABLED", "true")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = scrubbed_env()
    assert env["PATH"] == "/usr/bin"
    assert "HEALTH_DEEP_ENABLED" not in env
