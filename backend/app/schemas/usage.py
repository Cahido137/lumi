"""模型调用用量的数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class UsageMetadata(BaseModel):
    """模型单次调用返回的元数据。

    Note:
        字段以原始字典形式保存。
    """

    model_config = ConfigDict(extra="allow")

    input_tokens: int = Field(0, ge=0, description="本次调用消耗的输入token数")
    """本次调用消耗的输入token数。默认为0, 非负。"""

    output_tokens: int = Field(0, ge=0, description="本次调用产生的输出token数")
    """本次调用产生的输出token数。默认为0, 非负。"""

    total_tokens: int = Field(0, ge=0, description="总tokens数")
    """本次调用的总token数。默认为0, 非负。"""

    @classmethod
    def from_langchain_message(cls, usage: dict | None) -> UsageMetadata | None:
        """由 LangChain 消息的用量元数据构建用量对象。

        Args:
            usage: 消息所携带的 usage_metadata 字典, 可空。

        Returns:
            返回构建成功的用量对象。如果参数为空或字段校验失败则返回 None。
        """
        if not usage:
            return None
        try:
            return cls.model_validate(usage)  # 尝试转换
        except ValidationError:
            return None
