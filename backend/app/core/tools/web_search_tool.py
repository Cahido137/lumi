"""网络搜索工具"""

import html
import re
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from langchain_core.tools import tool


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
RESULTS_MAX_LEN = 5000
DEFAULT_MAX_RESULTS = 5
TIMEOUT = 10

# DuckDuckGo html 版结果: 链接 class=result__a, 摘要 class=result__snippet
_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
# Bing 结果块: <li class="b_algo">...</li>
_BING_BLOCK_RE = re.compile(r'<li class="b_algo".*?</li>', re.DOTALL)
_BING_LINK_RE = re.compile(r'<h2[^>]*><a[^>]+href="([^"]+)"[^>]*>(.*?)</a></h2>', re.DOTALL)
_BING_SNIPPET_RE = re.compile(r'<p[^>]*>(.*?)</p>', re.DOTALL)

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    """去除 HTML 标签并反转义"""
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _fetch(url: str) -> str:
    """发送 GET 请求, 返回解码后的文本"""
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=TIMEOUT) as res:
        raw = res.read()
        charset = res.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _resolve_ddg_url(href: str) -> str:
    """从 DuckDuckGo 跳转链接中取出真实地址"""
    url = href if href.startswith("http") else "https:" + href
    query = parse_qs(urlparse(url).query)
    real = query.get("uddg", [href])[0]
    return unquote(real)


def _search_ddg(query: str, max_results: int) -> list[dict]:
    """解析 DuckDuckGo 搜索结果"""
    page = _fetch("https://html.duckduckgo.com/html/?" + urlencode({"q": query}))
    results = []
    for m in _DDG_RESULT_RE.finditer(page):
        title, snippet = _clean(m.group(2)), _clean(m.group(3))
        if not title:
            continue
        results.append({
            "title": title,
            "url": _resolve_ddg_url(m.group(1)),
            "snippet": snippet,
        })
        if len(results) >= max_results:
            break
    return results


def _search_bing(query: str, max_results: int) -> list[dict]:
    """解析 Bing 搜索结果"""
    page = _fetch("https://www.bing.com/search?" + urlencode({"q": query}))
    results = []
    for block in _BING_BLOCK_RE.findall(page):
        link = _BING_LINK_RE.search(block)
        if not link:
            continue
        snippet_match = _BING_SNIPPET_RE.search(block)
        results.append({
            "title": _clean(link.group(2)),
            "url": html.unescape(link.group(1)),
            "snippet": _clean(snippet_match.group(1)) if snippet_match else "",
        })
        if len(results) >= max_results:
            break
    return results


@tool(parse_docstring=True)
def web_search(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> str:
    """
    在互联网上搜索信息, 返回结果标题、链接和摘要, 用于查询实时资讯、事实核查、获取公开资料。

    Args:
        query: 搜索关键词
        max_results: 返回的最大结果条数

    Returns:
        搜索结果列表, 或错误原因
    """
    try:
        try:
            results = _search_ddg(query, max_results)
        except Exception:
            results = []
        if not results:
            results = _search_bing(query, max_results)
        if not results:
            return "未找到搜索结果(可能被搜索引擎拦截), 请稍后重试或更换关键词"

        lines = [f"搜索关键词: {query}", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   链接: {r['url']}")
            if r["snippet"]:
                lines.append(f"   摘要: {r['snippet']}")
        text = "\n".join(lines)
        if len(text) > RESULTS_MAX_LEN:
            text = f"已截取前{RESULTS_MAX_LEN}字符: {text[:RESULTS_MAX_LEN]}"
        return text
    except Exception as e:
        # M1 大改: 搜索整体失败通过异常传播(由执行层统一转为 status="error" 的
        # 工具消息); 注意"未找到结果"仍算成功执行, 走上面的正常返回
        raise RuntimeError(f"搜索失败: {e}") from e
