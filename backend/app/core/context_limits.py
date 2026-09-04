"""上下文容量解析"""

import re
from dataclasses import dataclass
from functools import lru_cache

from app.config import get_compactsettings, get_llmsettings

# 已知模型的最大上下文注册表
MODEL_MAX_CONTEXT: list[tuple[str, int]] = [
    # OpenAI
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_047_576),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o4-mini", 200_000),
    ("o3", 200_000),
    # Anthropic
    ("claude", 200_000),
    # DeepSeek
    ("deepseek-chat", 65_536),
    ("deepseek-reasoner", 65_536),
    ("deepseek-v3", 65_536),
    ("deepseek-r1", 65_536),
    # 智谱 GLM
    ("glm-4.6", 128_000),
    ("glm-4.5", 128_000),
    ("glm-4-plus", 128_000),
    ("glm-4", 128_000),
    # 通义千问
    ("qwen-max", 32_768),
    ("qwen-plus", 131_072),
    ("qwen-turbo", 131_072),
    ("qwen-long", 10_000_000),
    ("qwen", 131_072),
    # Kimi / Moonshot
    ("kimi-k2", 131_072),
    ("kimi", 131_072),
]

# 模型名中自带容量后缀的识别
_K_SUFFIX = re.compile(r"(\d+)\s*k\b", re.IGNORECASE)
_M_SUFFIX = re.compile(r"(\d+)\s*m\b", re.IGNORECASE)


def detect_model_max_tokens(model_name: str | None) -> int | None:
    """按模型名推断最大上下文, 无法识别返回None"""
    if not model_name:
        return None
    name = model_name.strip().lower()
    # 名称自带容量后缀, 直接解析
    if m := _M_SUFFIX.search(name):
        return int(m.group(1)) * 1_000_000
    if m := _K_SUFFIX.search(name):
        return int(m.group(1)) * 1024
    # 注册表匹配, 最长前缀优先(如 glm-4.6 优先于 glm-4)
    for prefix, limit in sorted(MODEL_MAX_CONTEXT, key=lambda x: len(x[0]), reverse=True):
        if name.startswith(prefix):
            return limit
    return None


@lru_cache
def get_model_max_context() -> int:
    """获得当前主模型适用的最大上下文"""
    compact = get_compactsettings()
    if compact.compact_model_max_tokens:
        return compact.compact_model_max_tokens
    detected = detect_model_max_tokens(get_llmsettings().llm_model)
    if detected is not None:
        return detected
    return compact.compact_default_max_tokens


@dataclass(frozen=True)
class CompactLimits:
    """比例配置按当前模型最大上下文解析后的绝对阈值"""

    max_context_tokens: int  # 模型最大上下文
    trigger_tokens: int  # 触发压缩阈值
    warn_tokens: int  # 前端告警阈值
    keep_tokens: int  # 压缩后保留的近期消息token数


@lru_cache
def get_compact_limits() -> CompactLimits:
    """把比例配置解析为当前模型的绝对阈值"""
    settings = get_compactsettings()
    max_tokens = get_model_max_context()
    return CompactLimits(
        max_context_tokens=max_tokens,
        trigger_tokens=int(max_tokens * settings.compact_trigger_fraction),
        warn_tokens=int(max_tokens * settings.compact_warn_fraction),
        keep_tokens=int(max_tokens * settings.compact_keep_fraction),
    )
