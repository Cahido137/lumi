"""子进程执行环境的白名单构造。"""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings

import app.config as config_module
from app.config import get_workspacesettings

_POSIX_STRUCTURAL_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TMPDIR",
        "TZ",
    }
)
"""POSIX 平台子进程正常运行所必须的环境变量名。"""

_WINDOWS_STRUCTURAL_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
    }
)
"""Windows 平台子进程正常运行所必须的环境变量名。"""

_SENSITIVE_NAME_FRAGMENTS: tuple[str, ...] = (
    "SECRET",
    "TOKEN",
    "API_KEY",
    "APIKEY",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "DSN",
    "AUTH_SOCK",
)
"""变量名中带凭据语义的子串, 用于识别第三方凭证。"""


def structural_keys() -> frozenset[str]:
    """返回当前平台子进程正常运行所必须的环境变量名。"""
    return _WINDOWS_STRUCTURAL_KEYS if os.name == "nt" else _POSIX_STRUCTURAL_KEYS


@lru_cache
def own_config_keys() -> frozenset[str]:
    """自动枚举本项目全部配置类会读取的环境变量名。

    Returns:
        frozenset[str]: 受保护的变量名。
    """
    names: set[str] = set()
    for _, obj in vars(config_module).items():
        if not (isinstance(obj, type) and issubclass(obj, BaseSettings) and obj is not BaseSettings):
            continue
        prefix = str(obj.model_config.get("env_prefix", "") or "").upper()
        names |= {(prefix + field_name).upper() for field_name in obj.model_fields}
    return frozenset(names)


def is_sensitive_name(name: str) -> bool:
    """判断一个环境变量名是否属于不应透传的敏感项。

    Args:
        name: 环境变量名。

    Returns:
        bool: 属于本项目的配置项, 或者可能是凭据时返回 True。
    """
    upper = name.upper()
    return upper in own_config_keys() or any(fragment in upper for fragment in _SENSITIVE_NAME_FRAGMENTS)


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造可直接传给子进程的环境字典。

    Args:
        extra: 需要额外注入的变量。

    Returns:
        dict[str, str]: 可直接传给 subprocess.Popen(env=...) 的环境字典。
    """
    allowed = structural_keys() | {name.upper() for name in get_workspacesettings().workspace_env_allow}
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if not value:
            continue  # 空变量值直接丢弃
        if is_sensitive_name(key):
            continue
        if key.upper() not in allowed:
            continue
        env[key] = value
    if extra:
        env.update(extra)
    return env
