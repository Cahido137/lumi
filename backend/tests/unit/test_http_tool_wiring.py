"""http_get 工具与网络策略的接线测试(离线, 替换 fetch_text)。"""

import httpx
import pytest
from app.core.execution.net_policy import FetchResult, NetworkPolicy, use_network_policy
from app.core.tools import http_tool
from app.schemas.error_code import NetworkErrorCode
from app.utils.errors import NetworkPolicyViolation


def make_policy(**overrides) -> NetworkPolicy:
    base = {
        "enabled": True,
        "allowed_ports": frozenset({80, 443}),
        "host_allow": (),
        "host_deny": (),
        "allow_private": False,
        "max_redirects": 3,
        "max_response_bytes": 2048,
        "connect_timeout": 5.0,
        "read_timeout": 15.0,
    }
    base.update(overrides)
    return NetworkPolicy(**base)


def result(**overrides) -> FetchResult:
    base = {
        "status_code": 200,
        "text": "hello",
        "byte_size": 5,
        "truncated_bytes": False,
        "truncated_chars": False,
        "final_url": "http://example.com/",
        "redirect_count": 0,
    }
    base.update(overrides)
    return FetchResult(**base)


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """替换 fetch_text 并记录调用参数。"""
    seen: list[dict] = []

    async def fake_fetch(url, *, max_chars, policy=None, resolver=None, transport=None):
        seen.append({"url": url, "max_chars": max_chars, "policy": policy})
        return result()

    monkeypatch.setattr(http_tool, "fetch_text", fake_fetch)
    return seen


async def test_tool_passes_char_limit_and_bound_policy(calls: list[dict]) -> None:
    """工具把字符上限与当前上下文策略传给抓取函数。"""
    policy = make_policy()
    with use_network_policy(policy):
        text = await http_tool.http_get.ainvoke({"url": "http://example.com/"})
    assert calls[0]["max_chars"] == http_tool.BODY_MAX_LEN
    assert calls[0]["policy"] is policy
    assert text.startswith("响应: \n状态码: 200")
    assert "最终地址: http://example.com/" in text


async def test_tool_reports_byte_truncation(calls: list[dict], monkeypatch: pytest.MonkeyPatch) -> None:
    """字节截断的说明取策略里的字节上限。"""

    async def fake_fetch(url, **kwargs):
        return result(text="x" * 10, byte_size=2048, truncated_bytes=True)

    monkeypatch.setattr(http_tool, "fetch_text", fake_fetch)
    # 显式绑定策略: 截断说明里的字节上限来自策略, 不能依赖环境中的 .env
    with use_network_policy(make_policy(max_response_bytes=2048)):
        text = await http_tool.http_get.ainvoke({"url": "http://example.com/"})
    assert "[内容已截断, 上限2048字节, 实际读入2048字节]" in text


async def test_tool_reports_char_truncation(calls: list[dict], monkeypatch: pytest.MonkeyPatch) -> None:
    """字符截断的说明取工具层的字符上限。"""

    async def fake_fetch(url, **kwargs):
        return result(text="y" * 5000, byte_size=9000, truncated_chars=True)

    monkeypatch.setattr(http_tool, "fetch_text", fake_fetch)
    with use_network_policy(make_policy(max_response_bytes=2048)):
        text = await http_tool.http_get.ainvoke({"url": "http://example.com/"})
    assert f"[内容已截断, 上限{http_tool.BODY_MAX_LEN}字符, 实际读入9000字节]" in text


async def test_tool_byte_note_wins_when_both_truncated(calls: list[dict], monkeypatch: pytest.MonkeyPatch) -> None:
    """两个上限同时触发时文案报字节上限, 因为网络上还有内容没读。"""

    async def fake_fetch(url, **kwargs):
        return result(text="z" * 10, byte_size=2048, truncated_bytes=True, truncated_chars=True)

    monkeypatch.setattr(http_tool, "fetch_text", fake_fetch)
    with use_network_policy(make_policy(max_response_bytes=2048)):
        text = await http_tool.http_get.ainvoke({"url": "http://example.com/"})
    assert "[内容已截断, 上限2048字节, 实际读入2048字节]" in text


async def test_tool_does_not_wrap_policy_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    """策略违规必须原样传播, 否则稳定错误码会丢失。"""

    async def fake_fetch(url, **kwargs):
        raise NetworkPolicyViolation("主机解析到不允许访问的地址: x", code=NetworkErrorCode.ADDRESS_DENIED)

    monkeypatch.setattr(http_tool, "fetch_text", fake_fetch)
    with pytest.raises(NetworkPolicyViolation) as exc:
        await http_tool.http_get.ainvoke({"url": "http://169.254.169.254/"})
    assert exc.value.error_code == "address_denied"


async def test_tool_wraps_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """传输层错误仍按既有文案收敛为 RuntimeError。"""

    async def fake_fetch(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(http_tool, "fetch_text", fake_fetch)
    with pytest.raises(RuntimeError) as exc:
        await http_tool.http_get.ainvoke({"url": "http://example.com/"})
    assert str(exc.value).startswith("请求失败:")
