"""HTTP 协议工具"""

import httpx
from langchain_core.tools import tool

from app.core.execution.net_policy import FetchResult, NetworkPolicy, current_network_policy, fetch_text

BODY_MAX_LEN = 5000
"""回传给模型的最大字符数, 与策略中的字节上限独立生效。"""


def _truncate_note(result: FetchResult, policy: NetworkPolicy) -> str:
    """构造抓取结果的截断说明前缀。

    Args:
        result: 受限抓取的结果。
        policy: 生效的网络策略。

    Returns:
        str: 未截断时为空串, 否则为一行说明。
    """
    if not (result.truncated_bytes or result.truncated_chars):
        return ""
    # 字节上限意味着网络上还有内容没读, 字符上限意味着读全了但回传被裁, 模型对两者的后续动作不同
    limit = f"{policy.max_response_bytes}字节" if result.truncated_bytes else f"{BODY_MAX_LEN}字符"
    return f"[内容已截断, 上限{limit}, 实际读入{result.byte_size}字节]\n"


@tool(parse_docstring=True)
async def http_get(url: str) -> str:
    """
    使用 HTTP GET 请求获取网页或 API 内容, 用于查询公开接口、抓取网页文字。

    Args:
        url: 请求的完整 URL 地址

    Returns:
        GET 返回结果或错误原因
    """
    policy = current_network_policy()
    try:
        result = await fetch_text(url, max_chars=BODY_MAX_LEN, policy=policy)
    except httpx.HTTPError as e:
        # 只收敛传输层错误: 策略违规(NetworkPolicyViolation)必须原样传播, 否则会丢失稳定错误码
        raise RuntimeError(f"请求失败: {e}") from e
    return (
        f"{_truncate_note(result, policy)}响应: \n状态码: {result.status_code} "
        f"\n最终地址: {result.final_url} \n响应体: {result.text}"
    )
