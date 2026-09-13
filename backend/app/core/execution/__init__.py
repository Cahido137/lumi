"""受限执行模块的公共接口。

本包是工具与真实世界(文件系统、子进程)之间唯一的一层。
"""

from app.core.execution.env import scrubbed_env
from app.core.execution.workspace import (
    ReadResult,
    WorkspacePolicy,
    WriteResult,
    bind_workspace,
    current_workspace,
    read_text_bounded,
    reset_workspace,
    resolve_within_workspace,
    use_workspace,
    write_text_atomic,
)

__all__ = [
    "ReadResult",
    "WorkspacePolicy",
    "WriteResult",
    "bind_workspace",
    "current_workspace",
    "read_text_bounded",
    "reset_workspace",
    "resolve_within_workspace",
    "scrubbed_env",
    "use_workspace",
    "write_text_atomic",
]
