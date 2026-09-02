"""上下文容量解析单元测试"""

from types import SimpleNamespace

import pytest

import app.core.context_limits as context_limits
from app.core.context_limits import (
    detect_model_max_tokens,
    get_model_max_context,
    get_compact_limits,
)


@pytest.fixture(autouse=True)
def _clear_limits_cache():
    """lru_cache是模块级全局状态, 每个测试前后都清空防止互相污染"""
    context_limits.get_model_max_context.cache_clear()
    context_limits.get_compact_limits.cache_clear()
    yield
    context_limits.get_model_max_context.cache_clear()
    context_limits.get_compact_limits.cache_clear()


def _patch_settings(monkeypatch, *, max_tokens=None, default=64000, llm_model="some-unknown-model"):
    """把配置单例替换成测试替身, 避免读到真实.env影响断言"""
    monkeypatch.setattr(context_limits, "get_compactsettings", lambda: SimpleNamespace(
        compact_model_max_tokens=max_tokens,
        compact_default_max_tokens=default,
        compact_trigger_fraction=0.75,
        compact_warn_fraction=0.6,
        compact_keep_fraction=0.3,
    ))
    monkeypatch.setattr(context_limits, "get_llmsettings", lambda: SimpleNamespace(llm_model=llm_model))


# ---------- detect_model_max_tokens: 纯函数, 直接断言 ----------

def test_detect_empty_name_returns_none():
    """空模型名无法识别"""
    assert detect_model_max_tokens(None) is None
    assert detect_model_max_tokens("") is None


def test_detect_registry_prefix_match():
    """注册表按前缀匹配带版本后缀的完整模型名"""
    assert detect_model_max_tokens("gpt-4o-2024-08-06") == 128_000
    assert detect_model_max_tokens("claude-sonnet-4-5") == 200_000


def test_detect_longest_prefix_wins():
    """多个前缀都能匹配时取最长前缀(qwen-max优先于qwen兜底项)"""
    assert detect_model_max_tokens("qwen-max-latest") == 32_768
    assert detect_model_max_tokens("qwen-plus-latest") == 131_072


def test_detect_case_insensitive_and_strips():
    """匹配前先去掉首尾空格并转小写"""
    assert detect_model_max_tokens("  GPT-4o  ") == 128_000


def test_detect_k_suffix():
    """模型名自带128k容量后缀时直接解析, 优先级高于注册表"""
    assert detect_model_max_tokens("my-model-128k") == 128 * 1024


def test_detect_m_suffix():
    """模型名自带2m容量后缀时直接解析"""
    assert detect_model_max_tokens("llama-2m") == 2_000_000


def test_detect_unknown_returns_none():
    """注册表查不到且没有容量后缀返回None, 交给上层走默认值"""
    assert detect_model_max_tokens("my-secret-model") is None


# ---------- get_model_max_context: 三级优先级 ----------

def test_explicit_max_tokens_wins(monkeypatch):
    """配置文件手动指定最大上下文时优先生效, 不再探测模型名"""
    _patch_settings(monkeypatch, max_tokens=100_000, llm_model="gpt-4o")
    assert get_model_max_context() == 100_000


def test_detect_from_llm_model(monkeypatch):
    """未手动指定时按主模型名称自动探测"""
    _patch_settings(monkeypatch, llm_model="gpt-4o-2024-08-06")
    assert get_model_max_context() == 128_000


def test_fallback_to_default(monkeypatch):
    """探测不到时使用默认值兜底"""
    _patch_settings(monkeypatch, default=64_000, llm_model="my-secret-model")
    assert get_model_max_context() == 64_000


# ---------- get_compact_limits: 比例换算成绝对阈值 ----------

def test_compact_limits_fractions_to_tokens(monkeypatch):
    """比例配置按最大上下文换算成绝对阈值"""
    _patch_settings(monkeypatch, max_tokens=100_000)
    limits = get_compact_limits()
    assert limits.max_context_tokens == 100_000
    assert limits.trigger_tokens == 75_000
    assert limits.warn_tokens == 60_000
    assert limits.keep_tokens == 30_000


def test_compact_limits_cached(monkeypatch):
    """阈值解析结果被缓存, 重复调用返回同一对象"""
    _patch_settings(monkeypatch, max_tokens=100_000)
    assert get_compact_limits() is get_compact_limits()
