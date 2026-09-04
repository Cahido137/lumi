from pydantic import BaseModel, Field


class ContextUsageResponse(BaseModel):
    """上下文用量响应体"""

    used_tokens: int = Field(..., alias="usedTokens", description="当前上下文占用token数")
    max_context_tokens: int = Field(..., alias="maxContextTokens", description="当前模型最大上下文数")
    fraction: float = Field(..., description="上下文使用比例")
    warn_tokens: int = Field(..., alias="warnTokens", description="警告阈值token数")
    trigger_tokens: int = Field(..., alias="triggerTokens", description="自动压缩触发的阈值token数")
    message_count: int = Field(..., alias="messageCount", description="参与统计的消息条数")
    compacted: bool = Field(..., description="会话中是否已经有生效中的压缩摘要")


class ContextCompactResponse(BaseModel):
    """手动压缩结果响应体"""

    before_tokens: int = Field(..., alias="beforeTokens", description="压缩前上下文token数")
    after_tokens: int = Field(..., alias="afterTokens", description="压缩后上下文token数")
    summarized_message_count: int = Field(..., alias="summarizedMessageCount", description="被摘要覆盖的消息数")
