"""运行状态的流转规则。

Note:
    图的运行流转规则由本模块管理, 数据访问层与运行器都必须经过 ensure_transition 校验才能给 Run.status 赋值。
"""

from app.schemas.enums import RunStatus
from app.utils.errors import ConflictError

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {RunStatus.WAITING_APPROVAL, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.WAITING_APPROVAL: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}
"""合法流转表: 键表示当前状态, 值表示该状态允许进入的状态集合。"""

TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})
"""终态集合, 此集合内的状态进入后不再发生变化。"""


def is_terminal(status: RunStatus | str) -> bool:
    """判断一个运行状态是否为终态。

    Args:
        status: 运行状态。

    Returns:
        bool: 是否为终态。
    """
    return RunStatus(status) in TERMINAL_RUN_STATUSES


def ensure_transition(current: RunStatus | str, target: RunStatus | str) -> RunStatus:
    """校验一次状态流转是否合法。

    Args:
        current: 当前状态。
        target: 目标状态。

    Returns:
        RunStatus: 归一为枚举成员的目标状态。

    Raises:
        ConflictError: 该状态流转非法。
    """
    source = RunStatus(current)
    destination = RunStatus(target)
    if destination not in RUN_TRANSITIONS[source]:
        raise ConflictError(
            message=f"运行状态不可从 {source.value} 变更为 {destination.value}",
            detail={"from": source.value, "to": destination.value},
        )
    return destination
