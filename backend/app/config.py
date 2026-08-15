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


# 数据库配置信息
class DBSettings(BaseSettings):
    model_config = SettingsConfigDict(
            env_file=BASE_DIR / ".env", 
            env_file_encoding="utf-8",
            extra="ignore"
    )
    database_url: str = Field(..., description="数据库连接URL")

@lru_cache
def get_llmsettings() -> LLMSettings:
    return LLMSettings()

@lru_cache
def get_dbsettings() -> DBSettings:
    return DBSettings()