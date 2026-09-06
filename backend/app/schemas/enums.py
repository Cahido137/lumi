"""全局枚举定义: 包括事件类型、计划状态、审批状态、授权范围、执行状态、消息角色。

所有枚举类型均继承自StrEnum, 可直接作为字符串进行比较。数据库以字符串形式存储枚举值, 业务逻辑中以枚举常量进行判定。
"""
from enum import StrEnum


class EventType(StrEnum):
    """服务端向前端推送的事件类型。

    成员属性文档中说明该事件 data 字段的载荷结构 (使用字典)。

    Note:
        成员值为前端订阅时使用的事件名。
    """

    # 生命周期
    AGENT_STARTED = "agent_started"
    """运行开始事件。

    data 载荷为空对象。
    """

    AGENT_FINISHED = "agent_finished"
    """运行正常结束事件。

    data 载荷字段:
        reply: 本轮运行的完整回复文本。
    """

    ERROR = "error"
    """运行过程中出现错误事件。

    data 载荷字段:
        message: 错误说明文本。
    """

    RUN_CANCELLED = "run_cancelled"
    """运行被人工打断事件。

    data 载荷字段:
        message: 中断说明文本。
        message_id: 打断前已经生成的部分回复的消息ID, 未产生消息时设置为 None。
    """

    # 工具调用相关
    TOOL_STARTED = "tool_started"
    """工具开始执行事件。

    data 载荷字段:
        tool: 被调用的工具名称。
        tool_input: 本次工具调用的入参字典。
    """

    TOOL_FINISHED = "tool_finished"
    """工具结束执行事件。

    data 载荷字段:
        tool: 被调用的工具名称。
        tool_output: 工具的输出文本。
    """

    # 流式输出
    TOKEN = "token"
    """流式输出的增量文本事件。

    data 载荷字段:
        token: 本次增量的文本片段。
    """

    # 计划相关
    PLAN_UPDATED = "plan_updated"
    """计划表发生变更事件。

    data 载荷字段:
        todos: 变更后的完整计划步骤列表。
    """

    # 人工审批相关
    APPROVAL_REQUIRED = "approval_required"
    """工具调用等待人工审批事件。

    data 载荷字段:
        approval_id: 审批单ID。
        tool: 待审批的工具名称。
        tool_input: 待审批调用的入参字典。
    """

    APPROVAL_RESULT = "approval_result"
    """人工审批作出决定事件。

    data 载荷字段:
        approval_id: 审批单ID。
        status: 审批结果, 限定 approved 或 rejected。
    """

    # 上下文压缩相关
    CONTEXT_WARNING = "context_warning"
    """上下文用量达到警告阈值事件。

    data 载荷字段:
        used_tokens: 当前已用上下文token数。
        max_context_tokens: 模型上下文上限。
        fraction: 当前上下文使用比例。
        message: 警告说明文本。
    """

    CONTEXT_COMPACTED = "context_compacted"
    """上下文压缩完成事件。

    data 载荷字段:
        before_tokens: 压缩前的上下文token数。
        after_tokens: 压缩后的上下文token数。
        summarized_message_count: 被摘要覆盖的消息条数。
    """


class TodoStatus(StrEnum):
    """计划步骤状态。

    构成 pending -> in_progress -> done / failed 的流转关系。

    Note:
        此状态仅描述步骤的执行状态, 不标记步骤在计划列表中的位置。
    """

    PENDING = "pending"
    """步骤创建时的初始状态。"""

    IN_PROGRESS = "in_progress"
    """步骤已经开始执行。"""

    DONE = "done"
    """步骤已经执行完成。"""

    FAILED = "failed"
    """步骤因未获批准或执行未通过等原因而终止执行。"""


class ApprovalStatus(StrEnum):
    """审批单状态, 表示人工审批结果。

    Note:
        本枚举记录人工审批结果, 不表示工具执行结果。
    """

    PENDING = "pending"
    """等待审批状态。"""

    APPROVED = "approved"
    """审批同意状态。"""

    REJECTED = "rejected"
    """审批拒绝状态。"""


class ApprovalScope(StrEnum):
    """批准工具调用时所授予的授权范围。

    Note:
        命中持久授权的调用不再需要人工审批, 直接放行。
    """

    ONE_TIME = "one_time"
    """允许工具当次执行。"""

    COMMAND = "command"
    """允许此工具始终执行此命令。"""

    TOOL = "tool"
    """允许此工具始终执行。"""


class ExecutionStatus(StrEnum):
    """工具执行记录状态, 表示工具调用的实际处理结果。

    Note:
        本枚举表示工具执行结果。
    """

    PENDING = "pending"
    """等待审批状态。"""

    SUCCESS = "success"
    """执行成功状态。"""

    REJECTED = "rejected"
    """审批被拒状态。"""

    ERROR = "error"
    """执行出错状态。"""


class MessageRole(StrEnum):
    """消息角色, 用于区分消息的来源。"""

    USER = "user"
    """用户消息 (user)"""

    ASSISTANT = "assistant"
    """模型消息 (assistant)"""

    SYSTEM = "system"
    """系统提示词 (system)"""

    TOOL = "tool"
    """工具消息 (tool)"""
