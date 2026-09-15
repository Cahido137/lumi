"""受限执行模块的公共接口。

本包是工具与真实世界(文件系统、子进程、网络)之间唯一的一层。
"""

from app.core.execution.env import scrubbed_env
from app.core.execution.net_policy import (
    FetchResult,
    NetworkPolicy,
    ResolvedTarget,
    bind_network_policy,
    current_network_policy,
    default_network_policy,
    fetch_text,
    reset_network_policy,
    use_network_policy,
    validate_target,
)
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
    "FetchResult",
    "NetworkPolicy",
    "ReadResult",
    "ResolvedTarget",
    "WorkspacePolicy",
    "WriteResult",
    "bind_network_policy",
    "bind_workspace",
    "current_network_policy",
    "current_workspace",
    "default_network_policy",
    "fetch_text",
    "read_text_bounded",
    "reset_network_policy",
    "reset_workspace",
    "resolve_within_workspace",
    "scrubbed_env",
    "use_network_policy",
    "use_workspace",
    "validate_target",
    "write_text_atomic",
]
