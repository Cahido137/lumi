"""出站网络策略与受限抓取的单元测试。

全部用例离线运行: 主机名解析用假解析器替换, HTTP 传输用 httpx.MockTransport 替换。
"""

import codecs
import dataclasses
import gzip
import ipaddress
import socket

import httpx
import pytest
from app.config import get_networksettings
from app.core.execution.env import own_config_keys, scrubbed_env
from app.core.execution.net_policy import (
    NetworkPolicy,
    ResolvedTarget,
    current_network_policy,
    default_network_policy,
    fetch_text,
    host_matches,
    is_routable,
    resolve_host,
    use_network_policy,
    validate_target,
)
from app.utils.errors import NetworkPolicyViolation

PUBLIC_IP = "93.184.216.34"
"""示例用公网 IPv4。"""

PUBLIC_IP2 = "1.1.1.1"
"""第二个示例用公网 IPv4。"""

PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
"""示例用公网 IPv6。"""

ROUTABLE = [
    "8.8.8.8",
    "1.1.1.1",
    PUBLIC_IP,
    "192.88.99.1",
    PUBLIC_V6,
    "::ffff:8.8.8.8",
]
"""应被判定为可路由的地址。"""

BLOCKED = [
    "127.0.0.1",
    "::1",
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "fe80::1",
    "fc00::1",
    "100.64.0.1",
    "0.0.0.0",
    "::",
    "224.0.0.1",
    "ff02::1",
    "240.0.0.1",
    "198.18.0.1",
    "64:ff9b::a00:1",
    "2002:0a00:0001::1",
    "2001:db8::1",
    "100::1",
    "::ffff:10.0.0.1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
]
"""应被判定为不可路由的地址, 含 IPv4 映射与隧道形式。"""


def make_policy(**overrides) -> NetworkPolicy:
    """构造一份测试用网络策略, 未指定的项取安全默认值。

    Args:
        **overrides: 需要覆盖的策略字段。

    Returns:
        NetworkPolicy: 构造好的策略。
    """
    base = {
        "enabled": True,
        "allowed_ports": frozenset({80, 443}),
        "host_allow": (),
        "host_deny": (),
        "allow_private": False,
        "max_redirects": 3,
        "max_response_bytes": 2 * 1024 * 1024,
        "connect_timeout": 5.0,
        "read_timeout": 15.0,
    }
    base.update(overrides)
    return NetworkPolicy(**base)


def fake_resolver(mapping: dict[str, list[str]]):
    """构造把主机名映射到固定 IP 列表的解析器。

    Args:
        mapping: 主机名到 IP 字面量列表的映射, 未列出的主机名解析失败。

    Returns:
        Resolver: 可直接传给 validate_target / fetch_text 的解析器。
    """

    def resolve(host: str, port: int):
        if host not in mapping:
            raise socket.gaierror(-2, "Name or service not known")
        return [ipaddress.ip_address(item) for item in mapping[host]]

    return resolve


def never_resolver(host: str, port: int):
    """一个不允许被调用的解析器, 用于断言 IP 字面量主机不走解析。"""
    raise AssertionError("IP 字面量主机不应触发解析")


def recording_transport(routes: dict[str, httpx.Response], seen: list):
    """构造按连接 URL 分派响应并记录请求的传输层。

    Args:
        routes: 连接 URL 到响应的映射。
        seen: 用于收集请求的列表。

    Returns:
        httpx.MockTransport: 可传给 fetch_text 的传输层。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = str(request.url)
        if key not in routes:
            raise AssertionError(f"未预期的请求地址: {key}")
        return routes[key]

    return httpx.MockTransport(handler)


def violation_code(exc: NetworkPolicyViolation) -> str:
    """取出异常携带的稳定错误码。"""
    return exc.error_code


# --------------------------------------------------------------------------- #
# 地址类别判定
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ROUTABLE)
def test_routable_allows_public_addresses(raw: str) -> None:
    """公网单播地址应被放行。"""
    assert is_routable(ipaddress.ip_address(raw)) is True


@pytest.mark.parametrize("raw", BLOCKED)
def test_routable_blocks_non_public_addresses(raw: str) -> None:
    """回环、私网、链路本地、组播、保留与 IPv4 映射形式应全部被拦下。"""
    assert is_routable(ipaddress.ip_address(raw)) is False


def test_routable_blocks_cgnat_that_is_not_private() -> None:
    """运营商级 NAT 段不是 is_private, 必须靠 is_global 拦下。"""
    address = ipaddress.ip_address("100.64.0.1")
    assert address.is_private is False
    assert is_routable(address) is False


def test_routable_blocks_multicast_that_is_global() -> None:
    """组播地址的 is_global 为 True, 必须单独拦下。"""
    address = ipaddress.ip_address("224.0.0.1")
    assert address.is_global is True
    assert is_routable(address) is False


# --------------------------------------------------------------------------- #
# 主机名单匹配
# --------------------------------------------------------------------------- #


def test_host_matches_exact_and_case_insensitive() -> None:
    """精确匹配不区分大小写。"""
    assert host_matches("example.com", ("Example.COM",)) is True
    assert host_matches("example.com", ("other.com",)) is False


def test_host_matches_wildcard_only_covers_subdomains() -> None:
    """前缀通配只覆盖子域, 不覆盖裸域与形近域名。"""
    patterns = ("*.example.com",)
    assert host_matches("a.example.com", patterns) is True
    assert host_matches("a.b.example.com", patterns) is True
    assert host_matches("example.com", patterns) is False
    assert host_matches("evil-example.com", patterns) is False
    assert host_matches("evilexample.com", patterns) is False


def test_host_matches_ip_literal_and_empty_list() -> None:
    """IP 字面量按精确匹配处理, 空名单不命中任何主机。"""
    assert host_matches("127.0.0.1", ("127.0.0.1",)) is True
    assert host_matches("127.0.0.1", ()) is False


def test_host_matches_requires_lowercase_host() -> None:
    """名单项大小写无关, 主机名由调用方归一(validate_target 传的是 parsed.host)。"""
    assert host_matches("Example.com", ("example.com",)) is False
    assert host_matches("example.com", ("Example.COM",)) is True


# --------------------------------------------------------------------------- #
# 系统解析器
# --------------------------------------------------------------------------- #


def test_resolve_host_dedupes_and_keeps_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析结果去重且保持顺序。"""
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
    ]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
    assert [str(ip) for ip in resolve_host("example.com", 80)] == [PUBLIC_IP, PUBLIC_IP2]


def test_resolve_host_strips_ipv6_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """带作用域后缀的 IPv6 地址应被规范化。"""
    infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%en0", 80, 0, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
    assert [str(ip) for ip in resolve_host("link.local", 80)] == ["fe80::1"]


def test_resolve_host_propagates_gaierror(monkeypatch: pytest.MonkeyPatch) -> None:
    """无法解析时抛出 OSError 由调用方收敛。"""

    def boom(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(OSError):
        resolve_host("nope.invalid", 80)


# --------------------------------------------------------------------------- #
# 目标校验
# --------------------------------------------------------------------------- #

PUBLIC_RESOLVER = fake_resolver({"example.com": [PUBLIC_IP], "other.com": [PUBLIC_IP2]})


def test_validate_rejects_when_disabled() -> None:
    """总开关关闭时失败关闭。"""
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(make_policy(enabled=False), "http://example.com/", PUBLIC_RESOLVER)
    assert violation_code(exc.value) == "network_disabled"


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/a", "gopher://127.0.0.1:70/", "//example.com/x", "data:text/plain,hi"],
)
def test_validate_rejects_non_http_schemes(url: str) -> None:
    """非 http/https 协议一律拒绝。"""
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(make_policy(), url, PUBLIC_RESOLVER)
    assert violation_code(exc.value) == "scheme_denied"


@pytest.mark.parametrize("url", ["HTTP://Example.COM/Path", "Https://Example.COM/Path"])
def test_validate_accepts_uppercase_scheme_and_normalizes_host(url: str) -> None:
    """协议大小写不构成绕过, 主机名归一为小写。"""
    target = validate_target(make_policy(), url, PUBLIC_RESOLVER)
    assert target.host == "example.com"
    assert target.url.scheme == url.split(":")[0].lower()


@pytest.mark.parametrize("url", ["http://", "http:///path", "http:/\\/127.0.0.1", "https://:443/x"])
def test_validate_rejects_missing_host(url: str) -> None:
    """解析不出主机名的 URL 一律拒绝。"""
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(make_policy(), url, PUBLIC_RESOLVER)
    assert violation_code(exc.value) == "host_denied"


def test_validate_rejects_denied_host() -> None:
    """拒绝名单命中即拒。"""
    policy = make_policy(host_deny=("example.com",))
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(policy, "http://example.com/", PUBLIC_RESOLVER)
    assert violation_code(exc.value) == "host_denied"


def test_validate_deny_list_wins_over_allow_list() -> None:
    """同时命中两份名单时以拒绝为准。"""
    policy = make_policy(host_allow=("example.com",), host_deny=("example.com",))
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(policy, "http://example.com/", PUBLIC_RESOLVER)
    assert violation_code(exc.value) == "host_denied"


def test_validate_allow_list_bypasses_address_class_check() -> None:
    """放行名单命中时允许连接内网地址。"""
    policy = make_policy(host_allow=("localhost",), allowed_ports=frozenset({80, 443, 8080}))
    resolver = fake_resolver({"localhost": ["127.0.0.1"]})
    target = validate_target(policy, "http://localhost:8080/x", resolver)
    assert target.ip == "127.0.0.1"


def test_validate_allow_list_does_not_bypass_port_check() -> None:
    """放行主机名不等于放行它的所有端口。"""
    policy = make_policy(host_allow=("localhost",))
    resolver = fake_resolver({"localhost": ["127.0.0.1"]})
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(policy, "http://localhost:6379/", resolver)
    assert violation_code(exc.value) == "port_denied"


def test_validate_allow_list_wildcard_matches_subdomain() -> None:
    """放行名单支持显式前缀通配。"""
    policy = make_policy(host_allow=("*.corp.example",), allow_private=True)
    resolver = fake_resolver({"db.corp.example": ["10.0.0.5"]})
    target = validate_target(policy, "http://db.corp.example/", resolver)
    assert target.ip == "10.0.0.5"


def test_validate_strips_trailing_dot_before_matching() -> None:
    """主机名尾点归一后仍能命中名单。"""
    policy = make_policy(host_deny=("example.com",))
    with pytest.raises(NetworkPolicyViolation):
        validate_target(policy, "http://example.com./", PUBLIC_RESOLVER)


@pytest.mark.parametrize("url", ["http://example.com:8080/", "https://example.com:8443/", "http://example.com:22/"])
def test_validate_rejects_ports_outside_whitelist(url: str) -> None:
    """默认只放行 80 与 443。"""
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(make_policy(), url, PUBLIC_RESOLVER)
    assert violation_code(exc.value) == "port_denied"


def test_validate_accepts_configured_extra_port() -> None:
    """白名单里显式加入的端口可以连接。"""
    policy = make_policy(allowed_ports=frozenset({80, 443, 8080}))
    target = validate_target(policy, "http://example.com:8080/", PUBLIC_RESOLVER)
    assert target.port == 8080


@pytest.mark.parametrize(
    ("url", "expected_port"),
    [("http://example.com/", 80), ("https://example.com/", 443), ("http://example.com:80/", 80)],
)
def test_validate_infers_default_port(url: str, expected_port: int) -> None:
    """未写端口时按协议推断, 推断结果同样受白名单约束。"""
    assert validate_target(make_policy(), url, PUBLIC_RESOLVER).port == expected_port


@pytest.mark.parametrize(
    "resolved",
    [
        ["127.0.0.1"],
        ["::1"],
        ["169.254.169.254"],
        ["10.0.0.1"],
        ["192.168.1.1"],
        ["100.64.0.1"],
        ["224.0.0.1"],
        ["::ffff:10.0.0.1"],
        ["64:ff9b::a00:1"],
        ["0.0.0.0"],
    ],
)
def test_validate_blocks_non_routable_resolution(resolved: list[str]) -> None:
    """解析结果落在不可路由地址上时拒绝连接。"""
    resolver = fake_resolver({"evil.example": resolved})
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(make_policy(), "http://evil.example/", resolver)
    assert violation_code(exc.value) == "address_denied"
    assert exc.value.detail["resolved"] == resolved


def test_validate_allow_private_switch() -> None:
    """开发期开关打开后允许内网地址。"""
    resolver = fake_resolver({"internal.example": ["10.0.0.5"]})
    target = validate_target(make_policy(allow_private=True), "http://internal.example/", resolver)
    assert target.ip == "10.0.0.5"


def test_validate_picks_first_routable_from_mixed_answers() -> None:
    """多条解析记录里选出第一条可路由地址。"""
    resolver = fake_resolver({"mixed.example": ["10.0.0.1", PUBLIC_IP, "127.0.0.1"]})
    target = validate_target(make_policy(), "http://mixed.example/", resolver)
    assert target.ip == PUBLIC_IP


def test_validate_blocks_when_all_answers_are_internal() -> None:
    """全部解析结果都不可路由时拒绝。"""
    resolver = fake_resolver({"mixed.example": ["10.0.0.1", "127.0.0.1"]})
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(make_policy(), "http://mixed.example/", resolver)
    assert violation_code(exc.value) == "address_denied"
    assert len(exc.value.detail["resolved"]) == 2


def test_validate_dns_failure() -> None:
    """解析异常收敛为 dns_failed。"""
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(make_policy(), "http://nope.invalid/", fake_resolver({}))
    assert violation_code(exc.value) == "dns_failed"


def test_validate_empty_dns_answer() -> None:
    """解析成功但没有地址同样收敛为 dns_failed。"""
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(make_policy(), "http://empty.example/", lambda host, port: [])
    assert violation_code(exc.value) == "dns_failed"


def test_validate_ip_literal_skips_resolver() -> None:
    """主机本身就是 IP 字面量时不调用解析器。"""
    target = validate_target(make_policy(), f"http://{PUBLIC_IP}/x", never_resolver)
    assert target.ip == PUBLIC_IP


def test_validate_ip_literal_still_checked() -> None:
    """IP 字面量同样要过地址类别检查。"""
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(make_policy(), "http://169.254.169.254/latest/meta-data/", never_resolver)
    assert violation_code(exc.value) == "address_denied"


def test_validate_ipv6_literal_keeps_brackets_in_connect_url() -> None:
    """IPv6 字面量改写后仍带方括号。"""
    target = validate_target(make_policy(), f"http://[{PUBLIC_V6}]/x", never_resolver)
    assert target.ip == PUBLIC_V6
    assert str(target.connect_url) == f"http://[{PUBLIC_V6}]/x"


def test_validate_pins_ip_and_keeps_path_query() -> None:
    """连接地址改写为 IP, 路径与查询串保持不变。"""
    resolver = fake_resolver({"example.com": [PUBLIC_IP]})
    target = validate_target(make_policy(), "http://example.com/a/b?q=1#frag", resolver)
    assert str(target.connect_url) == f"http://{PUBLIC_IP}/a/b?q=1#frag"
    assert target.host_header == "example.com"


def test_validate_host_header_carries_explicit_port() -> None:
    """URL 显式写了端口时 Host 头要带上端口。"""
    policy = make_policy(allowed_ports=frozenset({80, 443, 8080}))
    target = validate_target(policy, "http://example.com:8080/x", PUBLIC_RESOLVER)
    assert target.host_header == "example.com:8080"


def test_validate_error_does_not_echo_url_credentials() -> None:
    """被拒绝时不回显原始 URL, 避免泄露 userinfo 形式的凭据。"""
    secret_url = f"http://user:hunter2@{PUBLIC_IP}/admin"
    with pytest.raises(NetworkPolicyViolation) as exc:
        validate_target(make_policy(enabled=False), secret_url, never_resolver)
    assert "hunter2" not in str(exc.value.message)
    assert "hunter2" not in repr(exc.value.to_payload())
    with pytest.raises(NetworkPolicyViolation) as exc2:
        validate_target(make_policy(host_deny=(PUBLIC_IP,)), secret_url, never_resolver)
    assert "hunter2" not in repr(exc2.value.to_payload())


def test_validate_target_is_frozen_dataclass() -> None:
    """校验结果不可变, 避免被下游改写。"""
    target = validate_target(make_policy(), "http://example.com/", PUBLIC_RESOLVER)
    assert isinstance(target, ResolvedTarget)
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.ip = "127.0.0.1"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 受限抓取
# --------------------------------------------------------------------------- #

RESOLVER = fake_resolver(
    {
        "example.com": [PUBLIC_IP],
        "other.com": [PUBLIC_IP2],
        "internal.example": ["127.0.0.1"],
        "loop.example": ["10.0.0.1"],
    }
)


async def test_fetch_returns_body_and_metadata() -> None:
    """正常响应返回状态码、文本与最终地址。"""
    seen: list[httpx.Request] = []
    transport = recording_transport({f"http://{PUBLIC_IP}/": httpx.Response(200, text="hello")}, seen)
    result = await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert result.status_code == 200
    assert result.text == "hello"
    assert result.byte_size == 5
    assert result.redirect_count == 0
    assert result.final_url == "http://example.com/"
    assert (result.truncated_bytes, result.truncated_chars) == (False, False)


async def test_fetch_connects_to_pinned_ip_with_original_host() -> None:
    """连接目标是已校验的 IP, Host 头与 SNI 仍是原主机名。"""
    seen: list[httpx.Request] = []
    transport = recording_transport({f"http://{PUBLIC_IP}/x": httpx.Response(200, text="ok")}, seen)
    await fetch_text("http://example.com/x", max_chars=100, resolver=RESOLVER, transport=transport)
    request = seen[0]
    assert request.url.host == PUBLIC_IP
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"


async def test_fetch_propagates_timeouts_from_policy() -> None:
    """策略里的分档超时被写入请求扩展。"""
    seen: list[httpx.Request] = []
    transport = recording_transport({f"http://{PUBLIC_IP}/": httpx.Response(200, text="ok")}, seen)
    policy = make_policy(connect_timeout=1.5, read_timeout=7.0)
    await fetch_text("http://example.com/", max_chars=10, policy=policy, resolver=RESOLVER, transport=transport)
    timeout = seen[0].extensions["timeout"]
    assert (timeout["connect"], timeout["read"]) == (1.5, 7.0)
    # write 与 pool 复用 connect 值是刻意的: 不为它们再开两个配置项
    assert (timeout["write"], timeout["pool"]) == (1.5, 1.5)


@pytest.mark.parametrize("status", [404, 429, 500, 503])
async def test_fetch_returns_error_status_without_raising(status: int) -> None:
    """4xx/5xx 作为正常响应返回, 由调用方区分业务成败。"""
    seen: list[httpx.Request] = []
    transport = recording_transport({f"http://{PUBLIC_IP}/": httpx.Response(status, text="boom")}, seen)
    result = await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert result.status_code == status
    assert result.text == "boom"


async def test_fetch_rejects_blocked_initial_target() -> None:
    """初始地址不合规时不发起任何连接。"""
    seen: list[httpx.Request] = []
    transport = recording_transport({}, seen)
    with pytest.raises(NetworkPolicyViolation) as exc:
        await fetch_text("http://internal.example/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert violation_code(exc.value) == "address_denied"
    assert seen == []


async def test_fetch_follows_redirect_to_public_host() -> None:
    """跨主机重定向逐跳重新解析与校验。"""
    seen: list[httpx.Request] = []
    routes = {
        f"http://{PUBLIC_IP}/": httpx.Response(302, headers={"location": "http://other.com/next"}),
        f"http://{PUBLIC_IP2}/next": httpx.Response(200, text="final"),
    }
    transport = recording_transport(routes, seen)
    result = await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert result.text == "final"
    assert result.redirect_count == 1
    assert result.final_url == "http://other.com/next"
    assert [request.url.host for request in seen] == [PUBLIC_IP, PUBLIC_IP2]
    assert [request.headers["host"] for request in seen] == ["example.com", "other.com"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_fetch_follows_every_redirect_status(status: int) -> None:
    """集合内的每个重定向状态码都会被跟随。"""
    seen: list[httpx.Request] = []
    routes = {
        f"http://{PUBLIC_IP}/": httpx.Response(status, headers={"location": "http://other.com/next"}),
        f"http://{PUBLIC_IP2}/next": httpx.Response(200, text="final"),
    }
    transport = recording_transport(routes, seen)
    result = await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert result.status_code == 200
    assert result.text == "final"
    assert result.redirect_count == 1


@pytest.mark.parametrize("status", [300, 304, 305])
async def test_fetch_does_not_follow_statuses_outside_redirect_set(status: int) -> None:
    """带 Location 但不在集合内的 3xx 按最终响应处理。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(status, headers={"location": "http://other.com/"}, text="stop")}
    transport = recording_transport(routes, seen)
    result = await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert result.status_code == status
    assert result.text == "stop"
    assert result.redirect_count == 0
    assert len(seen) == 1


async def test_fetch_blocks_redirect_to_internal_address() -> None:
    """重定向到内网地址被拦下。"""
    seen: list[httpx.Request] = []
    routes = {
        f"http://{PUBLIC_IP}/": httpx.Response(302, headers={"location": "http://internal.example/secret"}),
    }
    transport = recording_transport(routes, seen)
    with pytest.raises(NetworkPolicyViolation) as exc:
        await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert violation_code(exc.value) == "address_denied"
    assert len(seen) == 1


async def test_fetch_blocks_redirect_to_metadata_endpoint() -> None:
    """重定向到云元数据地址被拦下。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})}
    transport = recording_transport(routes, seen)
    with pytest.raises(NetworkPolicyViolation) as exc:
        await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert violation_code(exc.value) == "address_denied"


async def test_fetch_blocks_redirect_with_userinfo_and_hides_credentials() -> None:
    """重定向到带凭据的内网地址被拦下, 且凭据不进错误信封。"""
    seen: list[httpx.Request] = []
    routes = {
        f"http://{PUBLIC_IP}/": httpx.Response(
            302, headers={"location": "http://user:hunter2@internal.example/secret"}
        ),
    }
    transport = recording_transport(routes, seen)
    with pytest.raises(NetworkPolicyViolation) as exc:
        await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert violation_code(exc.value) == "address_denied"
    assert exc.value.detail["host"] == "internal.example"
    assert "hunter2" not in repr(exc.value.to_payload())


async def test_fetch_blocks_redirect_to_other_scheme() -> None:
    """重定向到非 http/https 协议被拦下。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(302, headers={"location": "file:///etc/passwd"})}
    transport = recording_transport(routes, seen)
    with pytest.raises(NetworkPolicyViolation) as exc:
        await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert violation_code(exc.value) == "scheme_denied"


async def test_fetch_blocks_redirect_to_denied_host_and_port() -> None:
    """重定向目标同样受主机名单与端口白名单约束。"""
    seen: list[httpx.Request] = []
    denied = {f"http://{PUBLIC_IP}/": httpx.Response(302, headers={"location": "http://blocked.example/"})}
    resolver = fake_resolver({**_resolver_map(), "blocked.example": [PUBLIC_IP2]})
    with pytest.raises(NetworkPolicyViolation) as exc:
        await fetch_text(
            "http://example.com/",
            max_chars=100,
            policy=make_policy(host_deny=("blocked.example",)),
            resolver=resolver,
            transport=recording_transport(denied, seen),
        )
    assert violation_code(exc.value) == "host_denied"

    bad_port = {f"http://{PUBLIC_IP}/": httpx.Response(302, headers={"location": f"http://{PUBLIC_IP2}:8080/"})}
    with pytest.raises(NetworkPolicyViolation) as exc2:
        await fetch_text(
            "http://example.com/",
            max_chars=100,
            resolver=RESOLVER,
            transport=recording_transport(bad_port, []),
        )
    assert violation_code(exc2.value) == "port_denied"


def _resolver_map() -> dict[str, list[str]]:
    """返回测试用的主机名到 IP 映射。"""
    return {
        "example.com": [PUBLIC_IP],
        "other.com": [PUBLIC_IP2],
        "internal.example": ["127.0.0.1"],
        "loop.example": ["10.0.0.1"],
    }


async def test_fetch_resolves_relative_location_against_original_host() -> None:
    """相对 Location 按本跳的原始 URL 拼接, 不会落到 IP 上。"""
    seen: list[httpx.Request] = []
    routes = {
        f"http://{PUBLIC_IP}/a": httpx.Response(301, headers={"location": "/b?x=1"}),
        f"http://{PUBLIC_IP}/b?x=1": httpx.Response(200, text="next"),
    }
    transport = recording_transport(routes, seen)
    result = await fetch_text("http://example.com/a", max_chars=100, resolver=RESOLVER, transport=transport)
    assert result.text == "next"
    assert result.final_url == "http://example.com/b?x=1"
    assert seen[1].headers["host"] == "example.com"


async def test_fetch_enforces_max_redirects() -> None:
    """跳数超过上限时拒绝继续跟随。"""
    seen: list[httpx.Request] = []
    routes = {
        f"http://{PUBLIC_IP}/": httpx.Response(302, headers={"location": "http://example.com/"}),
    }
    transport = recording_transport(routes, seen)
    with pytest.raises(NetworkPolicyViolation) as exc:
        await fetch_text(
            "http://example.com/",
            max_chars=100,
            policy=make_policy(max_redirects=2),
            resolver=RESOLVER,
            transport=transport,
        )
    assert violation_code(exc.value) == "too_many_redirects"
    assert len(seen) == 3


async def test_fetch_too_many_redirects_reports_only_location_host() -> None:
    """跳数超限的详情只回报主机名, 不回显服务器给的 Location 原文。"""
    seen: list[httpx.Request] = []
    routes = {
        f"http://{PUBLIC_IP}/": httpx.Response(302, headers={"location": "http://other.com/next"}),
        f"http://{PUBLIC_IP2}/next": httpx.Response(302, headers={"location": "http://user:hunter2@other.com/?t=abc"}),
    }
    transport = recording_transport(routes, seen)
    with pytest.raises(NetworkPolicyViolation) as exc:
        await fetch_text(
            "http://example.com/",
            max_chars=100,
            policy=make_policy(max_redirects=1),
            resolver=RESOLVER,
            transport=transport,
        )
    assert violation_code(exc.value) == "too_many_redirects"
    assert exc.value.detail["hops"] == 1
    assert len(seen) == 2
    assert exc.value.detail["location_host"] == "other.com"
    assert "location" not in exc.value.detail
    payload = repr(exc.value.to_payload())
    assert "hunter2" not in payload
    assert "t=abc" not in payload


async def test_fetch_malformed_location_surfaces_as_transport_error() -> None:
    """畸形 Location 由 httpx 在收响应头时抛传输错误, 不会进到策略层。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(302, headers={"location": "http://[::1"})}
    transport = recording_transport(routes, seen)
    with pytest.raises(httpx.HTTPError):
        await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)


async def test_fetch_zero_redirects_returns_3xx_as_final() -> None:
    """上限为 0 时不跟随, 3xx 原样返回。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(302, headers={"location": "http://other.com/"}, text="moved")}
    transport = recording_transport(routes, seen)
    result = await fetch_text(
        "http://example.com/",
        max_chars=100,
        policy=make_policy(max_redirects=0),
        resolver=RESOLVER,
        transport=transport,
    )
    assert result.status_code == 302
    assert result.text == "moved"
    assert result.redirect_count == 0
    assert len(seen) == 1


async def test_fetch_3xx_without_location_is_final() -> None:
    """3xx 但没有 Location 头时按最终响应处理。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(302, text="no location")}
    transport = recording_transport(routes, seen)
    result = await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert result.status_code == 302
    assert len(seen) == 1


async def test_fetch_truncates_at_byte_limit() -> None:
    """超出字节上限时停止读取并标记。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(200, content=b"x" * 6000)}
    transport = recording_transport(routes, seen)
    result = await fetch_text(
        "http://example.com/",
        max_chars=99999,
        policy=make_policy(max_response_bytes=5000),
        resolver=RESOLVER,
        transport=transport,
    )
    assert result.byte_size == 5000
    assert result.truncated_bytes is True
    assert result.truncated_chars is False


async def test_fetch_exact_byte_limit_is_not_truncated() -> None:
    """正好等于上限不算截断。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(200, content=b"x" * 5000)}
    transport = recording_transport(routes, seen)
    result = await fetch_text(
        "http://example.com/",
        max_chars=99999,
        policy=make_policy(max_response_bytes=5000),
        resolver=RESOLVER,
        transport=transport,
    )
    assert result.byte_size == 5000
    assert result.truncated_bytes is False


async def test_fetch_can_truncate_bytes_and_chars_together() -> None:
    """两个上限同时触发时两个标志都为真。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(200, content=b"x" * 6000)}
    transport = recording_transport(routes, seen)
    result = await fetch_text(
        "http://example.com/",
        max_chars=10,
        policy=make_policy(max_response_bytes=5000),
        resolver=RESOLVER,
        transport=transport,
    )
    assert result.byte_size == 5000
    assert result.text == "x" * 10
    assert (result.truncated_bytes, result.truncated_chars) == (True, True)


async def test_fetch_truncates_at_char_limit() -> None:
    """超出字符上限时裁掉尾部并标记。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(200, text="字" * 100)}
    transport = recording_transport(routes, seen)
    result = await fetch_text("http://example.com/", max_chars=10, resolver=RESOLVER, transport=transport)
    assert result.text == "字" * 10
    assert result.truncated_chars is True
    assert result.truncated_bytes is False
    assert result.byte_size == 300


async def test_fetch_byte_limit_counts_decoded_content() -> None:
    """字节上限按解压后的大小计算, 防止压缩炸弹。"""
    seen: list[httpx.Request] = []
    payload = gzip.compress(b"a" * 200_000)
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(200, content=payload, headers={"content-encoding": "gzip"})}
    transport = recording_transport(routes, seen)
    result = await fetch_text(
        "http://example.com/",
        max_chars=99999,
        policy=make_policy(max_response_bytes=10_000),
        resolver=RESOLVER,
        transport=transport,
    )
    assert len(payload) < 10_000
    assert result.byte_size == 10_000
    assert result.truncated_bytes is True


async def test_fetch_decodes_charset_from_headers() -> None:
    """按响应头声明的字符集解码。"""
    seen: list[httpx.Request] = []
    body = "中文内容".encode("gbk")
    routes = {
        f"http://{PUBLIC_IP}/": httpx.Response(200, content=body, headers={"content-type": "text/html; charset=gbk"})
    }
    transport = recording_transport(routes, seen)
    result = await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert result.text == "中文内容"


async def test_fetch_replaces_invalid_bytes_instead_of_raising() -> None:
    """非法字节按替换字符处理, 不抛解码异常。"""
    seen: list[httpx.Request] = []
    routes = {f"http://{PUBLIC_IP}/": httpx.Response(200, content=b"ok\xff\xfedata")}
    transport = recording_transport(routes, seen)
    result = await fetch_text("http://example.com/", max_chars=100, resolver=RESOLVER, transport=transport)
    assert result.text.startswith("ok")
    assert result.text.endswith("data")
    assert "\ufffd" in result.text


async def test_fetch_uses_bound_policy_by_default() -> None:
    """未显式传策略时取当前上下文绑定的策略。"""
    seen: list[httpx.Request] = []
    transport = recording_transport({}, seen)
    with use_network_policy(make_policy(enabled=False)):
        with pytest.raises(NetworkPolicyViolation) as exc:
            await fetch_text("http://example.com/", max_chars=10, resolver=RESOLVER, transport=transport)
    assert violation_code(exc.value) == "network_disabled"
    assert seen == []


async def test_fetch_propagates_transport_errors() -> None:
    """传输层错误按 httpx 异常传播, 不被策略异常吞掉。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(httpx.HTTPError):
        await fetch_text("http://example.com/", max_chars=10, resolver=RESOLVER, transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# 策略绑定与配置装配
# --------------------------------------------------------------------------- #


def test_use_network_policy_overrides_and_restores() -> None:
    """上下文绑定期间生效, 退出后还原。"""
    before = current_network_policy()
    custom = make_policy(max_redirects=0)
    with use_network_policy(custom) as bound:
        assert bound is custom
        assert current_network_policy().max_redirects == 0
    assert current_network_policy() is before


def test_bind_and_reset_restore_previous_policy() -> None:
    """令牌式绑定可以精确还原。"""
    from app.core.execution.net_policy import bind_network_policy, reset_network_policy

    original = current_network_policy()
    token = bind_network_policy(make_policy(enabled=False))
    assert current_network_policy().enabled is False
    reset_network_policy(token)
    assert current_network_policy() is original


def test_default_policy_reflects_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量装配进默认策略。"""
    monkeypatch.setenv("NETWORK_ENABLED", "false")
    monkeypatch.setenv("NETWORK_ALLOWED_PORTS", "80,443,8080")
    monkeypatch.setenv("NETWORK_HOST_ALLOW", "a.example,*.b.example")
    monkeypatch.setenv("NETWORK_HOST_DENY", '["c.example"]')
    monkeypatch.setenv("NETWORK_ALLOW_PRIVATE", "true")
    monkeypatch.setenv("NETWORK_MAX_REDIRECTS", "1")
    monkeypatch.setenv("NETWORK_MAX_RESPONSE_BYTES", "4096")
    monkeypatch.setenv("NETWORK_CONNECT_TIMEOUT", "2.5")
    monkeypatch.setenv("NETWORK_READ_TIMEOUT", "9.5")
    get_networksettings.cache_clear()
    default_network_policy.cache_clear()
    try:
        policy = default_network_policy()
        assert policy.enabled is False
        assert policy.allowed_ports == frozenset({80, 443, 8080})
        assert policy.host_allow == ("a.example", "*.b.example")
        assert policy.host_deny == ("c.example",)
        assert policy.allow_private is True
        assert policy.max_redirects == 1
        assert policy.max_response_bytes == 4096
        assert (policy.connect_timeout, policy.read_timeout) == (2.5, 9.5)
        assert current_network_policy() is policy
    finally:
        get_networksettings.cache_clear()
        default_network_policy.cache_clear()


def test_default_policy_uses_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置任何环境变量时默认拒绝内网、只放行 80 与 443。"""
    for name in (
        "NETWORK_ENABLED",
        "NETWORK_ALLOWED_PORTS",
        "NETWORK_HOST_ALLOW",
        "NETWORK_HOST_DENY",
        "NETWORK_ALLOW_PRIVATE",
        "NETWORK_MAX_REDIRECTS",
        "NETWORK_MAX_RESPONSE_BYTES",
        "NETWORK_CONNECT_TIMEOUT",
        "NETWORK_READ_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)
    get_networksettings.cache_clear()
    default_network_policy.cache_clear()
    try:
        policy = default_network_policy()
        assert policy.enabled is True
        assert policy.allowed_ports == frozenset({80, 443})
        assert policy.host_allow == ()
        assert policy.host_deny == ()
        assert policy.allow_private is False
        assert policy.max_redirects == 3
        assert policy.max_response_bytes == 2 * 1024 * 1024
        # connect 超时会被 httpcore 同时用于 TLS 握手, 默认值不得低于 10s
        assert (policy.connect_timeout, policy.read_timeout) == (10.0, 15.0)
    finally:
        get_networksettings.cache_clear()
        default_network_policy.cache_clear()


def test_network_settings_keys_are_scrubbed_from_subprocess_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络策略配置项不会透传给工具子进程。"""
    assert "NETWORK_HOST_ALLOW" in own_config_keys()
    assert "NETWORK_ALLOW_PRIVATE" in own_config_keys()
    monkeypatch.setenv("NETWORK_ALLOW_PRIVATE", "true")
    monkeypatch.setenv("PATH", "/usr/bin")
    assert "NETWORK_ALLOW_PRIVATE" not in scrubbed_env()


def test_codecs_lookup_of_supported_charsets() -> None:
    """解码依赖的字符集在本环境可用。"""
    for name in ("utf-8", "gbk", "iso-8859-1"):
        assert codecs.lookup(name) is not None
