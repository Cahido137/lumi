"""用量元数据模型(UsageMetadata)单元测试"""

import pytest
from app.schemas.usage import UsageMetadata
from pydantic import ValidationError


def test_core_fields_parsed():
    """三个核心字段正常解析"""
    usage = UsageMetadata(input_tokens=600, output_tokens=50, total_tokens=650)
    assert usage.input_tokens == 600
    assert usage.output_tokens == 50
    assert usage.total_tokens == 650


def test_missing_fields_default_to_zero():
    """缺失字段按0兜底, 不阻断"""
    usage = UsageMetadata(total_tokens=100)
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0


def test_negative_rejected():
    """负数token不合法, 直接拒绝"""
    with pytest.raises(ValidationError):
        UsageMetadata(input_tokens=-1, output_tokens=0, total_tokens=0)


def test_non_integer_rejected():
    """非整数类型不合法, 直接拒绝"""
    with pytest.raises(ValidationError):
        UsageMetadata(input_tokens="abc", output_tokens=0, total_tokens=0)


def test_extra_details_preserved():
    """供应商附带的缓存/推理明细字段原样保留不丢数据"""
    raw = {
        "input_tokens": 350,
        "output_tokens": 240,
        "total_tokens": 590,
        "input_token_details": {"cache_read": 100},
        "output_token_details": {"reasoning": 200},
    }
    usage = UsageMetadata.model_validate(raw)
    dumped = usage.model_dump()
    assert dumped["input_token_details"] == {"cache_read": 100}
    assert dumped["output_token_details"] == {"reasoning": 200}


def test_model_dump_roundtrip_to_jsonb():
    """序列化结果可直接进JSONB列"""
    usage = UsageMetadata(input_tokens=10, output_tokens=20, total_tokens=30)
    assert usage.model_dump() == {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}


# ---------- from_langchain_message: 防御性转换 ----------


def test_from_langchain_message_none():
    """None视为无数据"""
    assert UsageMetadata.from_langchain_message(None) is None


def test_from_langchain_message_valid():
    """合法usage_metadata正常转换"""
    usage = UsageMetadata.from_langchain_message({"input_tokens": 100, "output_tokens": 7, "total_tokens": 107})
    assert usage is not None
    assert usage.total_tokens == 107


def test_from_langchain_message_invalid_degrades_to_none():
    """非法数据(如字符串数字)降级为None而不是抛异常, 不影响对话主流程"""
    assert UsageMetadata.from_langchain_message({"input_tokens": "abc", "total_tokens": 1}) is None
