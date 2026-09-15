"""出站 HTTP 请求的网络策略与受限抓取。

工具入参中的 URL 一律先经本模块校验: 协议与端口必须在白名单内, 主机解析结果必须是可路由的
公网地址, 重定向逐跳重新校验。

Note:
    实际连接固定使用校验时解析出的 IP, 原主机名通过 Host 头与 TLS SNI 传递,
    避免校验与连接之间发生第二次解析。
"""

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from functools import lru_cache

import httpx

from app.config import get_networksettings
from app.schemas.error_code import NetworkErrorCode
from app.utils.errors import NetworkPolicyViolation

_READ_CHUNK_BYTES = 64 * 1024
"""流式读取单次读入的字节数。"""

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
"""需要跟随 Location 头的重定向状态码。"""

_ALLOWED_SCHEMES = frozenset({"http", "https"})
"""允许发起请求的协议。"""

_DEFAULT_PORTS = {"http": 80, "https": 443}
"""各协议的默认端口, URL 未显式指定端口时据此判断端口白名单。"""

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
"""一个已解析的 IP 地址。"""

Resolver = Callable[[str, int], list[IpAddress]]
"""主机名解析器签名: 接收主机名与端口, 返回候选 IP 地址列表。

Note:
    必须是同步函数, 本模块在 asyncio.to_thread 中调用它; 传入协程函数会得到不可迭代的 coroutine。
"""


@dataclass(frozen=True)
class NetworkPolicy:
    """一份出站访问策略。"""

    enabled: bool
    """是否允许发起出站请求。"""

    allowed_ports: frozenset[int]
    """允许连接的 TCP 端口。"""

    host_allow: tuple[str, ...]
    """额外放行的主机名或 IP 字面量, 命中即豁免地址类别检查。"""

    host_deny: tuple[str, ...]
    """无条件拒绝的主机名或 IP 字面量, 优先级高于 host_allow。"""

    allow_private: bool
    """是否放行不可路由地址。"""

    max_redirects: int
    """单次请求允许的最大重定向跳数。"""

    max_response_bytes: int
    """单次响应体读入的字节上限。"""

    connect_timeout: float
    """建立连接的超时秒数, 同时约束 TLS 握手。"""

    read_timeout: float
    """读取响应的超时秒数。"""


@dataclass(frozen=True)
class ResolvedTarget:
    """一个通过策略校验的出站目标。"""

    url: httpx.URL
    """调用方给出的原始 URL, 用于拼接相对形式的重定向地址。"""

    connect_url: httpx.URL
    """主机已改写为具体 IP 的 URL, 实际连接使用它。"""

    host: str
    """原始主机名, 已小写归一且不含端口。"""

    port: int
    """实际连接的 TCP 端口。"""

    ip: str
    """校验通过并用于连接的 IP 字面量。"""

    @property
    def host_header(self) -> str:
        """返回应写入 Host 头的值。

        Returns:
            str: 原始主机名, URL 显式写了端口时一并带上。
        """
        return f"{self.host}:{self.port}" if self.url.port is not None else self.host


@dataclass(frozen=True)
class FetchResult:
    """一次受限抓取的结果。"""

    status_code: int
    """最终响应的 HTTP 状态码。"""

    text: str
    """解码后的响应体文本, 可能被截断。"""

    byte_size: int
    """实际从网络读入的字节数, 已按内容编码解压。"""

    truncated_bytes: bool
    """是否因达到字节上限而停止读取。"""

    truncated_chars: bool
    """是否因达到字符上限而裁掉尾部文本。"""

    final_url: str
    """最终响应所在的 URL, 跟随重定向后可能与入参不同。"""

    redirect_count: int
    """本次请求跟随的重定向跳数。"""


def is_routable(ip: IpAddress) -> bool:
    """判断一个 IP 是否属于可路由的公网单播地址。

    Args:
        ip: 待判断的地址。

    Returns:
        bool: 允许直接连接时返回 True。
    """
    # 三个条件缺一不可: 组播地址的 is_global 为 True, 而运营商级 NAT 的 100.64.0.0/10 的 is_private 为 False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def resolve_host(host: str, port: int) -> list[IpAddress]:
    """解析主机名为候选 IP 地址列表。

    Args:
        host: 主机名。
        port: 目标端口, 仅作为解析的服务参数。

    Returns:
        list[IpAddress]: 去重并保持解析顺序的地址列表。

    Raises:
        OSError: 域名无法解析。

    Note:
        本函数为同步阻塞调用。
    """
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP, type=socket.SOCK_STREAM)
    # IPv6 的 sockaddr 可能带 %zone 后缀, ipaddress 不接受该形式
    raws = (str(info[4][0]).split("%", 1)[0] for info in infos)
    return list(dict.fromkeys(ipaddress.ip_address(raw) for raw in raws))


def host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    """判断主机名是否命中名单中的任意一项。

    Args:
        host: 小写主机名或 IP 字面量。
        patterns: 名单项, 支持 "*.example.com" 形式的显式前缀通配。

    Returns:
        bool: 命中任意一项时返回 True。
    """
    for pattern in (item.lower() for item in patterns):
        if not pattern.startswith("*."):
            if host == pattern:
                return True
            continue
        # 保留点号做边界: 只匹配子域, 既不匹配 example.com 自身, 也不匹配 evil-example.com
        suffix = pattern[1:]
        if host.endswith(suffix) and len(host) > len(suffix):
            return True
    return False


def _resolve_candidates(host: str, port: int, resolver: Resolver) -> list[IpAddress]:
    """取得一个主机的候选 IP 地址。

    Args:
        host: 主机名或 IP 字面量。
        port: 目标端口。
        resolver: 主机名解析器。

    Returns:
        list[IpAddress]: 候选地址, 主机本身就是 IP 字面量时不经解析直接返回。

    Raises:
        NetworkPolicyViolation: 解析失败或没有解析出任何地址。
    """
    try:
        literal: IpAddress | None = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None  # 不是 IP 字面量, 需要走解析器
    try:
        candidates = [literal] if literal is not None else resolver(host, port)
    except (OSError, ValueError) as e:
        raise NetworkPolicyViolation(
            f"主机名解析失败: {host}", code=NetworkErrorCode.DNS_FAILED, detail={"host": host}
        ) from e
    if not candidates:
        raise NetworkPolicyViolation(
            f"主机名没有解析出地址: {host}", code=NetworkErrorCode.DNS_FAILED, detail={"host": host}
        )
    return candidates


def validate_target(policy: NetworkPolicy, url: str, resolver: Resolver = resolve_host) -> ResolvedTarget:
    """校验一个 URL 是否允许访问, 并解析出实际连接地址。

    Args:
        policy: 生效的网络策略。
        url: 调用方给出的 URL。
        resolver: 主机名解析器, 默认为系统解析。

    Returns:
        ResolvedTarget: 通过校验的目标, 其 connect_url 的主机已是具体 IP。

    Raises:
        NetworkPolicyViolation: 协议、主机、端口或解析结果不符合策略。

    Note:
        本函数为同步阻塞调用, 在事件循环中应通过 asyncio.to_thread 执行。
        错误详情不回显原始 URL, 因为其中可能带有 userinfo 形式的凭据。
    """
    if not policy.enabled:
        raise NetworkPolicyViolation("出站网络访问已被禁用", code=NetworkErrorCode.NETWORK_DISABLED)

    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError) as e:
        raise NetworkPolicyViolation(
            "URL 无法解析", code=NetworkErrorCode.HOST_DENIED, detail={"reason": "url_unparsable", "error": str(e)}
        ) from e

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise NetworkPolicyViolation(
            f"仅允许 {'/'.join(sorted(_ALLOWED_SCHEMES))} 协议",
            code=NetworkErrorCode.SCHEME_DENIED,
            detail={"scheme": parsed.scheme},
        )

    host = parsed.host.rstrip(".")
    if not host:
        raise NetworkPolicyViolation(
            "URL 缺少主机名", code=NetworkErrorCode.HOST_DENIED, detail={"reason": "empty_host"}
        )

    if host_matches(host, policy.host_deny):
        raise NetworkPolicyViolation(
            f"主机在拒绝名单中: {host}", code=NetworkErrorCode.HOST_DENIED, detail={"host": host}
        )

    port = parsed.port or _DEFAULT_PORTS[parsed.scheme]
    if port not in policy.allowed_ports:
        raise NetworkPolicyViolation(
            f"端口不在白名单中: {port}", code=NetworkErrorCode.PORT_DENIED, detail={"host": host, "port": port}
        )

    # 命中放行名单的主机豁免地址类别检查, 否则内网服务永远无法为本地开发开放
    host_allowed = host_matches(host, policy.host_allow)
    candidates = _resolve_candidates(host, port, resolver)
    for ip in candidates:
        if host_allowed or policy.allow_private or is_routable(ip):
            return ResolvedTarget(
                url=parsed, connect_url=parsed.copy_with(host=str(ip)), host=host, port=port, ip=str(ip)
            )
    raise NetworkPolicyViolation(
        f"主机解析到不允许访问的地址: {host}",
        code=NetworkErrorCode.ADDRESS_DENIED,
        detail={"host": host, "resolved": [str(ip) for ip in candidates]},
    )


async def fetch_text(
    url: str,
    *,
    max_chars: int,
    policy: NetworkPolicy | None = None,
    resolver: Resolver = resolve_host,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchResult:
    """按策略抓取一个 URL 的响应体文本。

    Args:
        url: 目标 URL。
        max_chars: 允许回传的最大字符数。
        policy: 生效的网络策略, 为空时取当前上下文的策略。
        resolver: 主机名解析器, 默认为系统解析。
        transport: httpx 传输层实现, 为空时使用默认网络传输。

    Returns:
        FetchResult: 抓取结果, 状态码为 4xx/5xx 时同样正常返回。

    Raises:
        NetworkPolicyViolation: 初始地址或任意一跳重定向不符合策略。
        httpx.HTTPError: 连接失败、超时等传输层错误。

    Note:
        重定向由本函数逐跳跟随并重新校验, 因此 httpx 的自动重定向必须保持关闭。
    """
    active = policy if policy is not None else current_network_policy()
    # connect 超时会被 httpcore 同时用于 TCP 连接与 TLS 握手, 因此必须容得下完整握手过程
    timeout = httpx.Timeout(
        connect=active.connect_timeout,
        read=active.read_timeout,
        write=active.connect_timeout,
        pool=active.connect_timeout,
    )
    current = url
    hops = 0
    async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False) as client:
        while True:
            target = await asyncio.to_thread(validate_target, active, current, resolver)
            headers = {"Host": target.host_header}
            # 连接目标是 IP 字面量, TLS 握手仍需用原主机名做 SNI 与证书校验
            extensions = {"sni_hostname": target.host}
            async with client.stream("GET", target.connect_url, headers=headers, extensions=extensions) as response:
                location = response.headers.get("location")
                # 上限为 0 表示运维明确要求不跟随, 此时把 3xx 本身当作最终响应, 与"链路过长"区分开
                if location and response.status_code in _REDIRECT_STATUSES and active.max_redirects > 0:
                    if hops >= active.max_redirects:
                        raise NetworkPolicyViolation(
                            f"重定向跳数超过上限 {active.max_redirects}",
                            code=NetworkErrorCode.TOO_MANY_REDIRECTS,
                            # 只回报主机名: Location 由服务器给出, 原文可能带 userinfo 或任意长文本
                            detail={"hops": hops, "location_host": httpx.URL(location).host},
                        )
                    hops += 1
                    current = str(target.url.join(location))  # 相对地址按本跳的原始 URL 拼接
                    continue

                buffer = bytearray()
                truncated_bytes = False
                async for chunk in response.aiter_bytes(_READ_CHUNK_BYTES):
                    room = active.max_response_bytes - len(buffer)
                    if room <= 0 or len(chunk) > room:
                        buffer += chunk[: max(room, 0)]
                        truncated_bytes = True  # 满额之后仍收到数据, 说明还有内容没读
                        break
                    buffer += chunk
                text = bytes(buffer).decode(response.encoding or "utf-8", errors="replace")
                truncated_chars = len(text) > max_chars
                return FetchResult(
                    status_code=response.status_code,
                    text=text[:max_chars] if truncated_chars else text,
                    byte_size=len(buffer),
                    truncated_bytes=truncated_bytes,
                    truncated_chars=truncated_chars,
                    final_url=str(target.url),
                    redirect_count=hops,
                )


_network_var: ContextVar[NetworkPolicy | None] = ContextVar("network_policy", default=None)
"""当前上下文绑定的网络策略。"""


def bind_network_policy(policy: NetworkPolicy) -> Token[NetworkPolicy | None]:
    """把一份网络策略绑定到当前上下文。

    Args:
        policy: 要绑定的策略。

    Returns:
        Token: 用于 reset_network_policy 还原的令牌。
    """
    return _network_var.set(policy)


def reset_network_policy(token: Token[NetworkPolicy | None]) -> None:
    """还原此前绑定的网络策略。

    Args:
        token: bind_network_policy 返回的令牌。
    """
    _network_var.reset(token)


@contextmanager
def use_network_policy(policy: NetworkPolicy) -> Generator[NetworkPolicy, None, None]:
    """以 with 语句绑定网络策略, 退出时自动还原。

    Args:
        policy: 要绑定的策略。

    Yields:
        NetworkPolicy: 传入的策略本身。
    """
    token = bind_network_policy(policy)
    try:
        yield policy
    finally:
        reset_network_policy(token)


@lru_cache
def default_network_policy() -> NetworkPolicy:
    """构造进程级默认网络策略。

    Returns:
        NetworkPolicy: 由 NetworkSettings 构造的默认策略。
    """
    settings = get_networksettings()
    return NetworkPolicy(
        enabled=settings.network_enabled,
        allowed_ports=frozenset(settings.network_allowed_ports),
        host_allow=tuple(settings.network_host_allow),
        host_deny=tuple(settings.network_host_deny),
        allow_private=settings.network_allow_private,
        max_redirects=settings.network_max_redirects,
        max_response_bytes=settings.network_max_response_bytes,
        connect_timeout=settings.network_connect_timeout,
        read_timeout=settings.network_read_timeout,
    )


def current_network_policy() -> NetworkPolicy:
    """获取当前上下文生效的网络策略。

    Returns:
        NetworkPolicy: 已绑定的策略, 未绑定时返回进程级默认策略。
    """
    policy = _network_var.get()
    return policy if policy is not None else default_network_policy()
