"""深度体检相关, 包含执行、单飞、诊断结果缓存等。

Note:
    单飞与缓存都是进程内有效, 多进程时每个进程各持有一份缓存。
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_healthsettings
from app.core.checkpoint import ping_checkpoint
from app.core.llm import ping_chat_model, ping_provider
from app.db.session import SessionLocal
from app.schemas.health import CheckItem, DeepCheckResponse, DeepChecks

logger = logging.getLogger(__name__)

Probe = Callable[[], Awaitable[tuple[bool, str]]]
"""单项探针签名。

Note:
    原始详情仅用于日志记录, 不会随响应体返回至前端。
"""

Runner = Callable[[], Awaitable[DeepChecks]]
"""一次性完整深度体检签名。"""

DeepStatus = Literal["healthy", "degraded"]
"""深度体检的汇总状态。"""


def _elapsed_ms(started: float) -> float:
    """计算自 started 起的毫秒数, 保留两位小数。

    Args:
        started: time.monotonic() 取到的起始时刻。

    Returns:
        float: 耗时毫秒数。
    """
    return round((time.monotonic() - started) * 1000, 2)


def _item(name: str, ok: bool, ok_detail: str, failed_detail: str, latency_ms: float, raw: str) -> CheckItem:
    """把一个探针的执行结果收敛为对外可见的形式并将详情写入日志。

    Args:
        name: 检查项名称 (仅日志可见)。
        ok: 探针是否通过。
        ok_detail: 通过时对外展示的固定文案。
        failed_detail: 未通过时对外展示的固定文案。
        latency_ms: 本项检查耗时, 单位为毫秒。
        raw: 探针返回的原始数据 (仅日志可见)。

    Returns:
        CheckItem: 检查项结果。
    """
    if ok:
        logger.debug("深度体检项通过: name=%s, raw=%s", name, raw)
    else:
        logger.warning("深度体检项未通过: name=%s, raw=%s", name, raw)
    return CheckItem(ok=ok, detail=ok_detail if ok else failed_detail, latency_ms=latency_ms)


async def probe_database() -> tuple[bool, str]:
    """数据库可达性探针。

    Returns:
        tuple[bool, str]: (是否可达, 原始详情信息)。
    """
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except SQLAlchemyError as e:
        return False, f"{type(e).__name__}: {e}"
    return True, "SELECT 1 succeeded"


def _default_model_probe(model_timeout: float) -> Probe:
    """构造使用全局模型实例的模型生成探针。

    Args:
        model_timeout: 单次模型调用的超时时间, 单位秒。

    Returns:
        Probe: 无入参的模型生成探针。
    """

    async def probe() -> tuple[bool, str]:
        return await ping_chat_model(timeout=model_timeout)

    return probe


async def run_deep_checks(
    *,
    model_timeout: float,
    database_probe: Probe | None = None,
    checkpoint_probe: Probe | None = None,
    provider_probe: Probe | None = None,
    model_probe: Probe | None = None,
) -> DeepChecks:
    """串行执行探针并汇总结果。

    Args:
        model_timeout: 模型生成探针的超时时间, 单位秒。
        database_probe: 数据库可达性探针, 为空时使用 probe_database。
        checkpoint_probe: 检查点可达性探针, 为空时使用 ping_checkpoint。
        provider_probe: 模型供应商可达性探针, 为空时使用 ping_provider。
        model_probe: 模型生成探针, 为空时使用全局模型配置并套用 model_timeout。

    Returns:
        DeepChecks: 检查结果。

    Note:
        供应商不可达时会跳过模型生成探针的执行, 探针默认值一律在调用时解析。
    """
    # 声明实际使用的探针
    active_database_probe: Probe = database_probe if database_probe is not None else probe_database
    active_checkpoint_probe: Probe = checkpoint_probe if checkpoint_probe is not None else ping_checkpoint
    active_provider_probe: Probe = provider_probe if provider_probe is not None else ping_provider
    active_model_probe: Probe = model_probe if model_probe is not None else _default_model_probe(model_timeout)

    # 数据库可达性检查
    started = time.monotonic()
    ok, raw = await active_database_probe()
    database = _item("database", ok, "Database connected.", "Database unreachable.", _elapsed_ms(started), raw)

    # 检查点可达性检查
    started = time.monotonic()
    ok, raw = await active_checkpoint_probe()
    checkpoint = _item("checkpoint", ok, "Checkpoint available.", "Checkpoint unavailable.", _elapsed_ms(started), raw)

    # 模型供应商可达性检查
    started = time.monotonic()
    provider_ok, raw = await active_provider_probe()
    provider = _item("provider", provider_ok, "Provider reachable.", "Provider unreachable.", _elapsed_ms(started), raw)

    # 模型生成功能检查
    started = time.monotonic()
    if provider_ok:
        ok, raw = await active_model_probe()
        llm_generation = _item(
            "llm_generation", ok, "Model generation succeeded.", "Model generation failed.", _elapsed_ms(started), raw
        )
    else:
        logger.warning("供应商不可达, 无法进行模型生成功能检查")
        llm_generation = CheckItem.failed(
            detail="Skipped because the provider is unreachable.", latency_ms=_elapsed_ms(started)
        )
    return DeepChecks(database=database, checkpoint=checkpoint, provider=provider, llm_generation=llm_generation)


def summarize(checks: DeepChecks) -> DeepStatus:
    """汇总检查结果。

    Args:
        checks: 逐项检查结果。

    Returns:
        DeepStatus: 任意一项未通过即为 degraded。
    """
    items = (checks.database, checks.checkpoint, checks.provider, checks.llm_generation)
    return "degraded" if any(not item.ok for item in items) else "healthy"


@dataclass(frozen=True)
class DeepCheckSnapshot:
    """一份深度体检与它的产生时刻。"""

    checks: DeepChecks
    """逐项检查结果。"""

    status: DeepStatus
    """结果汇总状态。"""

    created_at: float
    """结果产生时刻。"""


class DeepCheckGuard:
    """深度体检的单飞闸门与短期结果缓存。

    Attributes:
        cache_ttl: 通过结果的复用秒数。
        failure_ttl: 降级结果的复用秒数。
    """

    def __init__(self, *, cache_ttl: float, failure_ttl: float, runner: Runner) -> None:
        """构造一个闸门。

        Args:
            cache_ttl: 通过结果的复用秒数。
            failure_ttl: 降级结果的复用秒数。
            runner: 真实执行一次深度体检的协程函数。
        """
        self.cache_ttl = cache_ttl
        self.failure_ttl = failure_ttl
        self._runner = runner
        self._lock = asyncio.Lock()
        self._snapshot: DeepCheckSnapshot | None = None

    def _ttl_of(self, snapshot: DeepCheckSnapshot) -> float:
        """返回一份结果适用的复用秒数。

        Args:
            snapshot: 待判断的结果。

        Returns:
            float: 通过结果使用 cache_ttl, 降级结果使用 failure_ttl。
        """
        return self.cache_ttl if snapshot.status == "healthy" else self.failure_ttl

    def _fresh(self, now: float) -> DeepCheckSnapshot | None:
        """取出仍在复用期内的结果。

        Args:
            now: 当前时刻。

        Returns:
            DeepCheckSnapshot | None: 已过期或尚无结果返回 None。
        """
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return snapshot if (now - snapshot.created_at) <= self._ttl_of(snapshot) else None

    def _response(self, snapshot: DeepCheckSnapshot, *, cached: bool) -> DeepCheckResponse:
        """将一份结果包装成响应体。

        Args:
            snapshot: 要返回的结果。
            cached: 本响应结果是否来自缓存复用。

        Returns:
            DeepCheckResponse: 响应体。
        """
        age_ms = round((time.monotonic() - snapshot.created_at) * 1000, 2)
        return DeepCheckResponse(status=snapshot.status, checks=snapshot.checks, cached=cached, age_ms=age_ms)

    async def get(self) -> DeepCheckResponse:
        """取得一份深度体检结果, 必要的时候会真实执行一次模型生成, 或使用缓存结果。

        Returns:
            DeepCheckResponse: 响应体。
        """
        snapshot = self._fresh(time.monotonic())
        if snapshot is not None:
            return self._response(snapshot, cached=True)
        async with self._lock:
            # 等锁期间可能别的调用方刷新过结果
            snapshot = self._fresh(time.monotonic())
            # 存在缓存
            if snapshot is not None:
                return self._response(snapshot, cached=True)
            checks = await self._runner()
            # 不存在缓存
            snapshot = DeepCheckSnapshot(checks=checks, status=summarize(checks), created_at=time.monotonic())
            self._snapshot = snapshot
            return self._response(snapshot, cached=False)


@lru_cache
def default_deep_check_guard() -> DeepCheckGuard:
    """构造进程级默认深度体检闸门。

    Returns:
        DeepCheckGuard: 由 HealthSettings 配置的闸门对象。
    """
    settings = get_healthsettings()

    async def runner() -> DeepChecks:
        return await run_deep_checks(model_timeout=settings.health_deep_model_timeout)

    return DeepCheckGuard(
        cache_ttl=settings.health_deep_cache_ttl, failure_ttl=settings.health_deep_failure_ttl, runner=runner
    )


async def deep_check(guard: DeepCheckGuard | None = None) -> DeepCheckResponse:
    """执行一次深度体检。

    Args:
        guard: 使用的深度体检闸门, 为空时采用默认配置。

    Returns:
        DeepCheckResponse: 深度体检响应体。
    """
    active = guard if guard is not None else default_deep_check_guard()
    return await active.get()
