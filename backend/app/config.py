from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


# 根据当前文件位置找到根目录绝对位置
BASE_DIR = Path(__file__).resolve().parent.parent


# 基础大模型配置信息
class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", 
        env_file_encoding="utf-8",
        extra="ignore"
    )

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
    model_config = SettingsConfigDict(
            env_file=BASE_DIR / ".env", 
            env_file_encoding="utf-8",
            extra="ignore"
    )
    database_url: str = Field(..., description="数据库连接URL")


# 认证配置信息
class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", 
        env_file_encoding="utf-8",
        extra="ignore"
    )
    jwt_secret: str = Field(..., description="JWT签名密钥")
    jwt_algorithm: str = Field("HS256", description="JWT签名算法")
    jwt_expire_days: int = Field(7, gt=0, description="Token有效期")


# 网络搜索工具配置
class WebSearchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", 
        env_file_encoding="utf-8",
        extra="ignore"
    )
    web_search_provider: str = Field("tavily", description="搜索服务商(default: Tavily)")
    web_search_api_key: str | None = Field(None, description="网络搜索服务API密钥")
    web_search_base_url: str = Field("https://api.tavily.com/search", description="网络搜索服务接口地址")

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