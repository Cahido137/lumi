"""会话运行上下文数据"""

from dataclasses import dataclass

from app.core.graph.schemas import ApprovalInterrupt
from app.schemas.usage import UsageMetadata


# 运行上下文
@dataclass
class RunContext:
    config: dict
    thread_id: str


# 图执行结果
@dataclass
class StreamResult:
    final_reply: str
    interrupt: ApprovalInterrupt | None = None
    final_usage: UsageMetadata | None = None
