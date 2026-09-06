"""上下文管理接口的响应模型。"""

from pydantic import BaseModel, Field


class ContextUsageResponse(BaseModel):
    """上下文用量响应体。"""

    used_tokens: int = Field(..., alias="usedTokens", description="当前上下文占用token数")
    """当前上下文占用token数。为估算值而非精确值, 根据字符与token间的固定比例计算得出。"""

    max_context_tokens: int = Field(..., alias="maxContextTokens", description="当前模型最大上下文数")
    """当前模型最大上下文数。"""

    fraction: float = Field(..., description="上下文使用比例")
    """上下文使用比例。取值为 0-1。"""

    warn_tokens: int = Field(..., alias="warnTokens", description="警告阈值token数")
    """警告阈值token数。由模型最大上下文乘以警告阈值比例得出。"""

    trigger_tokens: int = Field(..., alias="triggerTokens", description="自动压缩触发的阈值token数")
    """触发自动上下文压缩的阈值token数。恒大于 warn_tokens。"""

    message_count: int = Field(..., alias="messageCount", description="参与统计的消息条数")
    """参与统计的消息条数。统计范围为系统提示词与重建后的历史消息数之和。"""

    compacted: bool = Field(..., description="会话中是否已经有生效中的压缩摘要")
    """会话中是否已经有生效中的压缩摘要。"""


class ContextCompactResponse(BaseModel):
    """手动压缩结果响应体。"""

    before_tokens: int = Field(..., alias="beforeTokens", description="压缩前上下文token数")
    """压缩前的上下文token数。与压缩发起时的用量一致。"""

    after_tokens: int = Field(..., alias="afterTokens", description="压缩后上下文token数")
    """压缩后的上下文token数。"""

    summarized_message_count: int = Field(..., alias="summarizedMessageCount", description="被摘要覆盖的消息数")
    """被摘要操作覆盖的消息数。"""
