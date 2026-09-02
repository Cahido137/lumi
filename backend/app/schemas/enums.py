from enum import Enum


class EventType(str, Enum):
    """事件类型枚举"""
    # 生命周期
    AGENT_STARTED = "agent_started"  # data: {}
    AGENT_FINISHED = "agent_finished"  # data: {"reply": 回复}
    ERROR = "error"  # data: {"message": 错误消息}
    RUN_CANCELLED = "run_cancelled"  # data: {"message_id": 中断说明, "message_id": 部分回复的消息ID}

    # 工具调用相关
    TOOL_STARTED = "tool_started"  # data: {"tool": 工具, "tool_input": 工具输入}
    TOOL_FINISHED = "tool_finished"  # data: {"tool": 工具, "tool_output": 工具输出}

    # 流式输出
    TOKEN = "token"  # data: {"token": 本token内容}

    # todo
    PLAN_UPDATED = "plan_updated"  # data: {"todos": [完整todo列表]}

    # 人工审批相关
    APPROVAL_REQUIRED = "approval_required"  # data: {"approval_id": 审批ID, "tool": 待审批工具, "tool_input": 工具输入}
    APPROVAL_RESULT = "approval_result"  # data: {"approval_id": 审批ID, "status": "approved"/"rejected"}

    # 上下文压缩相关
    CONTEXT_WARNING = "context_warning"  # data: {"used_tokens": 当前已用上下文tokens, "max_context_tokens": 模型最大上下文, "fractions": 使用比例, "message": 警告消息}
    CONTEXT_COMPACTED = "context_compacted"  # data: {"before_tokens": 压缩前上下文tokens, "after_tokens": 压缩后上下文tokens, "summarized_message_count": 被摘要消息数}

class TodoStatus(str, Enum):
    """todo 状态枚举"""
    PENDING = "pending"  # 未执行
    IN_PROGRESS = "in_progress"  # 正在执行
    DONE = "done"  # 已执行
    FAILED = "failed"  # 失败

class ApprovalStatus(str, Enum):
    """审批单状态"""
    PENDING = "pending"  # 等待审批
    APPROVED = "approved"  # 审批同意
    REJECTED = "rejected"  # 审批拒绝

class ApprovalScope(str, Enum):
    """工具审批权限"""
    ONE_TIME = "one_time"  # 允许执行一次
    COMMAND = "command"  # 允许此工具始终执行此命令
    TOOL = "tool"  # 允许此工具始终执行

class ExecutionStatus(str, Enum):
    """工具执行状态"""
    PENDING = "pending"  # 等待审批
    SUCCESS = "success"  # 执行成功
    REJECTED = "rejected"  # 审批被拒
    ERROR = "error"  # 执行出错

class MessageRole(str, Enum):
    """消息角色"""
    USER = "user"  # 用户消息
    ASSISTANT = "assistant"  # 模型消息
    SYSTEM = "system"  # 系统提示词
    TOOL = "tool"  # 工具消息