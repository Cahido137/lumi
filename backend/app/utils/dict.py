"""字典归一化工具。"""

import json
from typing import Any


def normalize_dict(data: dict[str, Any] | None) -> str:
    """把字典归一化为可比较的 JSON 字符串。

    Args:
        data: 待归一化的字典, 可空。

    Returns:
        str: 归一化后的 JSON 字符串。
    """
    if data is None:
        data = {}
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
