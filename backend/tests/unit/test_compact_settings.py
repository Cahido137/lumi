"""上下文压缩配置(CompactSettings)单元测试"""

import pytest
from app.config import CompactSettings, get_compactsettings
from pydantic import ValidationError


def _make_settings(**overrides):
    """构造一套合法配置作为基准, 每个测试只覆盖自己关心的字段"""
    base = dict(
        compact_enabled=True,
        compact_trigger_fraction=0.75,
        compact_warn_fraction=0.6,
        compact_keep_fraction=0.3,
        compact_model_max_tokens=None,
        compact_default_max_tokens=64000,
    )
    base.update(overrides)
    return CompactSettings(**base)


def test_valid_fractions_accepted():
    """触发>警告、触发>保留的合法比例可以正常构造"""
    settings = _make_settings()
    assert settings.compact_enabled is True
    assert settings.compact_trigger_fraction == 0.75
    assert settings.compact_warn_fraction == 0.6
    assert settings.compact_keep_fraction == 0.3


def test_warn_fraction_equal_to_trigger_rejected():
    """警告比例等于触发比例时报错: 必须先警告再压缩"""
    with pytest.raises(ValidationError):
        _make_settings(compact_warn_fraction=0.75)


def test_warn_fraction_greater_than_trigger_rejected():
    """警告比例大于触发比例时报错"""
    with pytest.raises(ValidationError):
        _make_settings(compact_warn_fraction=0.9)


def test_keep_fraction_equal_to_trigger_rejected():
    """保留比例等于触发比例时报错: 压缩后上下文必须明显变小"""
    with pytest.raises(ValidationError):
        _make_settings(compact_keep_fraction=0.75)


def test_keep_fraction_greater_than_trigger_rejected():
    """保留比例大于触发比例时报错"""
    with pytest.raises(ValidationError):
        _make_settings(compact_keep_fraction=0.8)


def test_fraction_zero_rejected():
    """比例为0没有意义, 被字段约束(gt=0)拦截"""
    with pytest.raises(ValidationError):
        _make_settings(compact_warn_fraction=0.0)


def test_fraction_greater_than_one_rejected():
    """比例超过1没有意义, 被字段约束(le=1.0)拦截"""
    with pytest.raises(ValidationError):
        _make_settings(compact_trigger_fraction=1.2)


def test_get_compactsettings_is_singleton():
    """单例获取函数两次调用返回同一对象, 保证全局读到的配置一致"""
    assert get_compactsettings() is get_compactsettings()
