from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class UsageMetadata(BaseModel):
    """模型单次调用返回的元数据"""

    model_config = ConfigDict(extra="allow")

    input_tokens: int = Field(0, ge=0, description="本次调用消耗的输入token数")
    output_tokens: int = Field(0, ge=0, description="本次调用产生的输出token数")
    total_tokens: int = Field(0, ge=0, description="总tokens数")

    @classmethod
    def from_langchain_message(cls, usage: dict | None) -> UsageMetadata | None:
        """从langchain消息元数据提取此模型类"""
        if not usage:
            return None
        try:
            return cls.model_validate(usage)  # 尝试转换
        except ValidationError:
            return None
