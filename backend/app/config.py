"""分领域的配置信息定义与读取。

每个配置领域对应一个继承自 BaseSettings 的子类, 均以根目录下的 .env 作为配置来源。

Note:
    配置信息为单例模式获取, 使用 lru_cache 缓存结果。如果要修改配置文件需重启进程生效。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 根据当前文件位置找到根目录绝对位置
BASE_DIR = Path(__file__).resolve().parent.parent


class LLMSettings(BaseSettings):
    """大模型连接与生成参数配置。"""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    llm_base_url: str = Field(..., description="大模型URL地址")
    """大模型的URL地址。必填项。"""

    llm_api_key: str = Field(..., description="大模型api_key")
    """大模型的api密钥。必填项。"""

    llm_model: str = Field(..., description="模型名")
    """模型名。必填项。"""

    llm_temperature: float = Field(0.7, description="模型温度")
    """模型温度。默认为 0.7。"""

    llm_provider: str = Field("openai", description="模型提供商")
    """模型提供商。默认为 openai。"""

    llm_max_tokens: int | None = Field(None, description="单次回复最大token数")
    """单次回复最大token数。为 None 时由服务端默认策略决定。"""

    llm_timeout: float = Field(60.0, description="单次请求超时秒数")
    """单次请求超时秒数。默认为 60.0s。"""

    llm_max_retries: int = Field(2, ge=0, description="最大重试次数")
    """最大重试次数。默认为 2, 非负。"""


class DBSettings(BaseSettings):
    """数据库配置信息。"""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(..., description="数据库连接URL")
    """数据库连接URL。此URL直接用于创建异步引擎。"""


class AuthSettings(BaseSettings):
    """用户令牌认证配置信息。"""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    jwt_secret: str = Field(..., description="JWT签名密钥")
    """JWT签名密钥。变更后此前签发的令牌将全部无法通过校验。"""

    jwt_algorithm: str = Field("HS256", description="JWT签名算法")
    """JWT签名算法。默认为 HS256。"""

    jwt_expire_days: int = Field(7, gt=0, description="Token有效期")
    """Token有效期。默认为 7 天。正整数。"""


class WebSearchSettings(BaseSettings):
    """联网搜索配置信息。"""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    web_search_provider: str = Field("tavily", description="搜索服务商(default: Tavily)")
    """搜索服务商。默认为 Tavily。"""

    web_search_api_key: str | None = Field(None, description="网络搜索服务API密钥")
    """网络搜索服务API密钥。"""

    web_search_base_url: str = Field("https://api.tavily.com/search", description="网络搜索服务接口地址")
    """网络搜索服务接口地址。默认为 Tavily 官方接口地址。"""


class LogSettings(BaseSettings):
    """日志配置信息。"""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = Field("INFO", description="日志等级")
    """日志等级。默认为 INFO。"""

    database_echo: bool = Field(False, description="是否打印SQL语句")
    """是否打印SQL语句。默认为 False。"""


class OpsSettings(BaseSettings):
    """运维与诊断信息配置信息。"""

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    debug_error_detail: bool = Field(False, description="错误响应是否要携带异常类型与堆栈信息")
    """错误响应是否要附带调试信息。"""


class CompactSettings(BaseSettings):
    """上下文压缩配置信息。

    Note:
        比例相关参数的合法关系由 _validate_fractions 函数在实例构造完成后校验。
    """

    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    compact_enabled: bool = Field(True, description="是否启用上下文压缩")
    """是否启用上下文压缩。默认为 True。"""

    compact_trigger_fraction: float = Field(0.75, gt=0, le=1.0, description="自动触发压缩的上下文比例")
    """自动触发压缩的上下文比例。默认为 0.75, 取值为 0-1。"""

    compact_warn_fraction: float = Field(0.6, gt=0, le=1.0, description="触发上下文警告的上下文比例")
    """触发上下文警告的上下文比例。默认为 0.6, 须严格小于自动触发压缩的上下文比例。"""

    compact_keep_fraction: float = Field(0.3, gt=0, le=1.0, description="压缩后保留原文的上下文比例")
    """压缩后保留原文的上下文比例。默认为 0.3, 须严格小于自动触发压缩的上下文比例。"""

    compact_model_max_tokens: int | None = Field(None, gt=0, description="当前模型的最大上下文")
    """当前模型的最大上下文。"""

    compact_default_max_tokens: int = Field(64000, gt=0, description="默认的上下文")
    """默认上下文。默认为 64000。"""

    @model_validator(mode="after")
    def _validate_fractions(self):
        """校验压缩相关比例之间的大小关系。

        Returns:
            CompactSettings: 校验通过后的实例本身。

        Raises:
            ValueError: 当警告比例或保留比例不小于触发比例时抛出。
        """
        if self.compact_trigger_fraction <= self.compact_warn_fraction:
            raise ValueError("compact_warn_fraction must be less than compact_trigger_fraction")
        if self.compact_trigger_fraction <= self.compact_keep_fraction:
            raise ValueError("compact_keep_fraction must be less than compact_trigger_fraction")
        return self


@lru_cache
def get_llmsettings() -> LLMSettings:
    """获得大模型配置单例。

    Returns:
        LLMSettings: 进程内唯一配置实例。
    """
    return LLMSettings()


@lru_cache
def get_dbsettings() -> DBSettings:
    """获得数据库配置单例。

    Returns:
        DBSettings: 进程内唯一配置实例。
    """
    return DBSettings()


@lru_cache
def get_authsettings() -> AuthSettings:
    """获得认证配置单例。

    Returns:
        AuthSettings: 进程内唯一配置实例。
    """
    return AuthSettings()


@lru_cache
def get_web_search_settings() -> WebSearchSettings:
    """获得联网搜索配置单例。

    Returns:
        WebSearchSettings: 进程内唯一配置实例。
    """
    return WebSearchSettings()


@lru_cache
def get_logsettings() -> LogSettings:
    """获得日志配置单例。

    Returns:
        LogSettings: 进程内唯一配置实例。
    """
    return LogSettings()


@lru_cache
def get_opssettings() -> OpsSettings:
    """获得运维与诊断配置单例。

    Returns:
        OpsSettings: 进程内唯一配置实例。
    """
    return OpsSettings()


@lru_cache
def get_compactsettings() -> CompactSettings:
    """获得上下文压缩配置单例。

    Returns:
        CompactSettings: 进程内唯一配置单例, 构造时已完成合法性校验。
    """
    return CompactSettings()
