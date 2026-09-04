from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 根据当前文件位置找到根目录绝对位置
BASE_DIR = Path(__file__).resolve().parent.parent


# 基础大模型配置信息
class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    llm_base_url: str = Field(..., description="大模型URL地址")
    llm_api_key: str = Field(..., description="大模型api_key")
    llm_model: str = Field(..., description="模型名")
    llm_temperature: float = Field(0.7, description="模型温度")

    llm_provider: str = Field("openai", description="模型提供商")
    llm_max_tokens: int | None = Field(None, description="单次回复最大token数")
    llm_timeout: float = Field(60.0, description="单次请求超时秒数")
    llm_max_retries: int = Field(2, ge=0, description="最大重试次数")


# 数据库配置信息
class DBSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")
    database_url: str = Field(..., description="数据库连接URL")


# 认证配置信息
class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")
    jwt_secret: str = Field(..., description="JWT签名密钥")
    jwt_algorithm: str = Field("HS256", description="JWT签名算法")
    jwt_expire_days: int = Field(7, gt=0, description="Token有效期")


# 网络搜索工具配置
class WebSearchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")
    web_search_provider: str = Field("tavily", description="搜索服务商(default: Tavily)")
    web_search_api_key: str | None = Field(None, description="网络搜索服务API密钥")
    web_search_base_url: str = Field("https://api.tavily.com/search", description="网络搜索服务接口地址")


class LogSettings(BaseSettings):
    """日志配置"""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")
    log_level: str = Field("INFO", description="日志等级")
    database_echo: bool = Field(False, description="是否打印SQL语句")


class CompactSettings(BaseSettings):
    """上下文压缩配置"""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")
    compact_enabled: bool = Field(True, description="是否启用上下文压缩")
    compact_trigger_fraction: float = Field(0.75, gt=0, le=1.0, description="自动触发压缩的上下文比例")
    compact_warn_fraction: float = Field(0.6, gt=0, le=1.0, description="触发上下文警告的上下文比例")
    compact_keep_fraction: float = Field(0.3, gt=0, le=1.0, description="压缩后保留原文的上下文比例")
    compact_model_max_tokens: int | None = Field(None, gt=0, description="当前模型的最大上下文")
    compact_default_max_tokens: int = Field(64000, gt=0, description="默认的上下文")

    @model_validator(mode="after")
    def _validate_fractions(self):
        """检查警告比例和自动触发压缩比例是否满足指定大小关系"""
        if self.compact_trigger_fraction <= self.compact_warn_fraction:
            raise ValueError("compact_warn_fraction must be less than compact_trigger_fraction")
        if self.compact_trigger_fraction <= self.compact_keep_fraction:
            raise ValueError("compact_keep_fraction must be less than compact_trigger_fraction")
        return self


@lru_cache
def get_llmsettings() -> LLMSettings:
    """获得大模型配置单例"""
    return LLMSettings()


@lru_cache
def get_dbsettings() -> DBSettings:
    """获得数据库配置单例"""
    return DBSettings()


@lru_cache
def get_authsettings() -> AuthSettings:
    """获得认证配置单例"""
    return AuthSettings()


@lru_cache
def get_web_search_settings() -> WebSearchSettings:
    """获得网络搜索配置单例"""
    return WebSearchSettings()


@lru_cache
def get_logsettings() -> LogSettings:
    """获得日志配置单例"""
    return LogSettings()


@lru_cache
def get_compactsettings() -> CompactSettings:
    """获得上下文压缩配置单例"""
    return CompactSettings()
