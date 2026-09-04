"""网络搜索工具"""

import asyncio

import httpx
from langchain_core.tools import tool

from app.config import get_web_search_settings

RESULTS_MAX_LEN = 5000
DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_HARD_CAP = 20
# 分档超时: 连接 10s, 读取放宽到 25s
TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=5.0)
MAX_RETRIES = 2  # 超时/瞬时服务端错误最多重试 2 次
RETRY_BACKOFF = 1.5  # 重试退避基数(秒)


async def _search_with_tavily(
    client: httpx.AsyncClient, url: str, api_key: str, query: str, max_results: int
) -> list[dict]:
    """Tavily 搜索适配"""
    res = await client.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
    )
    res.raise_for_status()
    return res.json().get("results") or []


async def _request_with_retry(
    client: httpx.AsyncClient, url: str, api_key: str, query: str, max_results: int
) -> list[dict]:
    """带重试的搜索请求: 超时与瞬时服务端错误(429/5xx)退避重试, 其余错误直接抛"""
    last_err: httpx.TimeoutException | httpx.HTTPStatusError | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await _search_with_tavily(client, url, api_key, query, max_results)
        except httpx.TimeoutException as e:
            last_err = e  # 超时是主要失败形态, 值得重试
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 500, 502, 503):
                last_err = e  # 限流/瞬时服务端错误, 可重试
            else:
                raise  # 401/402/403 等不可重试, 直接抛
        if attempt < MAX_RETRIES:
            await asyncio.sleep(RETRY_BACKOFF * (attempt + 1))
    if last_err is None:
        raise RuntimeError("搜索失败: 未记录到任何错误")
    raise last_err


@tool(parse_docstring=True)
async def web_search(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> str:
    """
    在互联网上搜索信息, 返回结果标题、链接和摘要, 用于查询实时资讯、事实核查、获取公开资料。

    Args:
        query: 搜索关键词
        max_results: 返回的最大结果条数

    Returns:
        搜索结果列表
    """
    settings = get_web_search_settings()
    if not settings.web_search_api_key:
        # 未配置密钥
        raise RuntimeError("未配置搜索服务API密钥")

    # 限制搜索结果数量
    max_results = max(1, min(int(max_results), MAX_RESULTS_HARD_CAP))

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            # 按服务商分派到对应适配函数
            provider = settings.web_search_provider
            if provider == "tavily":
                results = await _request_with_retry(
                    client, settings.web_search_base_url, settings.web_search_api_key, query, max_results
                )
            else:
                raise RuntimeError(f"不支持的搜索服务商: {provider}")

        if not results:
            # 引擎正常工作但无结果, 视为成功返回
            return "未找到搜索结果, 请更换关键词重试"

        lines = [f"搜索关键词: {query}", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.get('title', '')}")
            lines.append(f"   链接: {r.get('url', '')}")
            snippet = (r.get("content") or "").strip()
            if snippet:
                lines.append(f"   摘要: {snippet}")
        text = "\n".join(lines)
        if len(text) > RESULTS_MAX_LEN:
            text = f"已截取前{RESULTS_MAX_LEN}字符: {text[:RESULTS_MAX_LEN]}"
        return text
    except httpx.TimeoutException:
        # 超时异常
        raise RuntimeError("搜索请求超时, 已重试仍失败, 请稍后重试") from None
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"搜索请求失败(状态码{e.response.status_code}): {str(e.response.text)[:200]}") from e
    except Exception as e:
        raise RuntimeError(f"搜索失败: {e}") from e
