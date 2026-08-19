import json


def normalize_dict(data: dict) -> str:
    """将字典序列化为json字符串"""
    if data is None:
        data = {}
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)