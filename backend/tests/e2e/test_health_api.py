"""E2E测试: 健康检查路由的鉴权、缓存复用与详情收敛(需要测试库, 不触碰真实供应商)。"""

import asyncio

import httpx
import pytest
from app.config import get_healthsettings, get_llmsettings
from app.core import diagnostics
from app.main import app

HEALTH_ENV_KEYS = (
    "HEALTH_DEEP_ENABLED",
    "HEALTH_DEEP_CACHE_TTL",
    "HEALTH_DEEP_FAILURE_TTL",
    "HEALTH_DEEP_MODEL_TIMEOUT",
)
"""本层新增的配置项环境变量名。"""


@pytest.fixture(autouse=True)
def _reset_diagnostic_caches(monkeypatch: pytest.MonkeyPatch):
    """每个用例前后清掉配置与闸门缓存, 避免结果跨用例复用。"""
    for name in HEALTH_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    get_healthsettings.cache_clear()
    diagnostics.default_deep_check_guard.cache_clear()
    yield
    get_healthsettings.cache_clear()
    diagnostics.default_deep_check_guard.cache_clear()


@pytest.fixture()
async def client():
    """ASGI传输: 直接调app对象, 不经过网络"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
def fake_probes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """替换四个探针: 不出网、不调真实模型, 并记录各自的调用次数。"""
    calls = {"database": 0, "checkpoint": 0, "provider": 0, "model": 0}

    def named(name: str):
        async def _probe() -> tuple[bool, str]:
            calls[name] += 1
            return True, f"{name} raw detail"

        return _probe

    async def fake_model(llm=None, timeout: float = 15.0) -> tuple[bool, str]:
        calls["model"] += 1
        return True, f"Connected. LLM return: ping (timeout={timeout})"

    monkeypatch.setattr(diagnostics, "probe_database", named("database"))
    monkeypatch.setattr(diagnostics, "ping_checkpoint", named("checkpoint"))
    monkeypatch.setattr(diagnostics, "ping_provider", named("provider"))
    monkeypatch.setattr(diagnostics, "ping_chat_model", fake_model)
    return calls


def auth_header(token_data) -> dict[str, str]:
    """构造登录后的请求头。"""
    return {"Authorization": f"Bearer {token_data['accessToken']}"}


async def register_user(client, username: str = "health_user") -> dict:
    """注册一个用户并返回令牌载荷。"""
    res = await client.post("/api/auth/register", json={"username": username, "password": "pass1234"})
    assert res.status_code == 200
    return res.json()["data"]


async def test_liveness_and_readiness_stay_public(client) -> None:
    """存活与就绪探针不需要登录, 容器编排仍可直接调用。"""
    live = await client.get("/api/health")
    assert live.status_code == 200
    assert live.json()["data"]["status"] == "ok"
    ready = await client.get("/api/health/ready")
    assert ready.status_code == 200
    assert ready.json()["data"]["checks"]["database"]["ok"] is True


async def test_deep_rejects_anonymous_and_bad_token(client, fake_probes) -> None:
    """未登录与令牌无效都返回 401, 且不触发任何探针。"""
    anonymous = await client.get("/api/health/deep")
    assert anonymous.status_code == 401
    assert anonymous.json()["data"] is None
    bad = await client.get("/api/health/deep", headers={"Authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401
    assert sum(fake_probes.values()) == 0


async def test_deep_allows_any_registered_user(client, fake_probes) -> None:
    """任意已落库用户都能做深度体检, 四项检查结果齐全。"""
    token = await register_user(client)
    res = await client.get("/api/health/deep", headers=auth_header(token))
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["status"] == "healthy"
    assert data["cached"] is False
    assert set(data["checks"]) == {"database", "checkpoint", "provider", "llm_generation"}
    assert fake_probes == {"database": 1, "checkpoint": 1, "provider": 1, "model": 1}


async def test_deep_second_call_hits_cache(client, fake_probes) -> None:
    """TTL 内的第二次调用复用结果, 不再触发模型调用。"""
    token = await register_user(client)
    first = await client.get("/api/health/deep", headers=auth_header(token))
    second = await client.get("/api/health/deep", headers=auth_header(token))
    assert first.json()["data"]["cached"] is False
    assert second.json()["data"]["cached"] is True
    assert second.json()["data"]["age_ms"] >= 0.0
    assert fake_probes == {"database": 1, "checkpoint": 1, "provider": 1, "model": 1}


async def test_deep_concurrent_calls_run_probes_once(client, fake_probes) -> None:
    """并发请求被合并, 真实执行只发生一次。"""
    token = await register_user(client)
    headers = auth_header(token)
    responses = await asyncio.gather(*(client.get("/api/health/deep", headers=headers) for _ in range(5)))
    assert [res.status_code for res in responses] == [200] * 5
    assert fake_probes["model"] == 1
    assert [res.json()["data"]["cached"] for res in responses].count(False) == 1


async def test_deep_runs_real_database_and_checkpoint_probes(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """数据库与检查点探针走真实依赖, 只替换会花钱的供应商与模型探针。"""

    async def fake_provider() -> tuple[bool, str]:
        return True, "供应商可达"

    async def fake_model(llm=None, timeout: float = 15.0) -> tuple[bool, str]:
        return True, "Connected."

    monkeypatch.setattr(diagnostics, "ping_provider", fake_provider)
    monkeypatch.setattr(diagnostics, "ping_chat_model", fake_model)
    token = await register_user(client, username="health_real_probe")
    res = await client.get("/api/health/deep", headers=auth_header(token))
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["checks"]["database"]["ok"] is True
    assert data["checks"]["database"]["detail"] == "Database connected."
    assert data["checks"]["checkpoint"]["ok"] is True
    assert data["status"] == "healthy"


async def test_deep_can_be_disabled_by_config(client, fake_probes, monkeypatch: pytest.MonkeyPatch) -> None:
    """配置关闭后对已登录用户返回 403 与稳定错误码, 且不触发任何探针。"""
    token = await register_user(client)
    monkeypatch.setenv("HEALTH_DEEP_ENABLED", "false")
    get_healthsettings.cache_clear()
    res = await client.get("/api/health/deep", headers=auth_header(token))
    assert res.status_code == 403
    body = res.json()
    assert body["code"] == 403
    assert body["data"]["error_code"] == "forbidden"
    assert sum(fake_probes.values()) == 0


async def test_deep_still_requires_auth_when_disabled(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """开关关闭不改变鉴权顺序: 匿名请求仍是 401 而不是 403。"""
    monkeypatch.setenv("HEALTH_DEEP_ENABLED", "false")
    get_healthsettings.cache_clear()
    res = await client.get("/api/health/deep")
    assert res.status_code == 401


async def test_deep_detail_does_not_leak_provider_config(client, fake_probes, monkeypatch: pytest.MonkeyPatch) -> None:
    """探针原文里的供应商地址与密钥不会出现在响应体中。"""
    settings = get_llmsettings()

    async def leaking_provider() -> tuple[bool, str]:
        return False, f"模型提供商未提供模型列表接口: {settings.llm_base_url}/models key={settings.llm_api_key}"

    monkeypatch.setattr(diagnostics, "ping_provider", leaking_provider)
    token = await register_user(client)
    res = await client.get("/api/health/deep", headers=auth_header(token))
    assert res.status_code == 200
    assert settings.llm_base_url not in res.text
    assert settings.llm_api_key not in res.text
    data = res.json()["data"]
    assert data["status"] == "degraded"
    assert data["checks"]["provider"]["detail"] == "Provider unreachable."
    assert data["checks"]["llm_generation"]["detail"] == "Skipped because the provider is unreachable."
    assert fake_probes["model"] == 0
