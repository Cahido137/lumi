"""HTTP 协议工具"""

import httpx

from langchain_core.tools import tool


BODY_MAX_LEN = 5000

@tool(parse_docstring=True)
async def http_get(url: str) -> str:
    """
    使用 HTTP GET 请求获取网页或 API 内容, 用于查询公开接口、抓取网页文字。

    Args:
        url: 请求的完整 URL 地址

    Returns:
        GET 返回结果或错误原因
    """
    # 仅允许 http 和 https 协议
    if not url.startswith(("http://", "https://")):
        return "拦截。仅允许使用http和https协议"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            res = await client.get(url)
            body = res.text
            if len(body) > BODY_MAX_LEN:
                body = f"已截取前{BODY_MAX_LEN}字符: {body[:BODY_MAX_LEN]}"
            return f"响应: \n状态码: {res.status_code} \n响应体: {body}"
    except Exception as e:
        return f"请求失败: {e}"