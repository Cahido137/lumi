"""运行状态机的单元测试(离线, 不触碰数据库)。"""

import pytest
from app.core.run_state import RUN_TRANSITIONS, TERMINAL_RUN_STATUSES, ensure_transition, is_terminal
from app.schemas.enums import RunStatus
from app.utils.errors import ConflictError

LEGAL_PAIRS = sorted((src.value, dst.value) for src, allowed in RUN_TRANSITIONS.items() for dst in allowed)
"""流转表中登记的全部合法 (源状态, 目标状态) 组合。"""

ILLEGAL_PAIRS = sorted(
    (src.value, dst.value) for src in RunStatus for dst in RunStatus if dst not in RUN_TRANSITIONS[src]
)
"""全部非法的 (源状态, 目标状态) 组合, 含自反流转。"""


@pytest.mark.parametrize(("source", "target"), LEGAL_PAIRS)
def test_legal_transition_is_accepted(source: str, target: str) -> None:
    """流转表登记的每一条都通过校验, 并返回归一后的枚举成员。"""
    assert ensure_transition(source, target) is RunStatus(target)


@pytest.mark.parametrize(("source", "target"), ILLEGAL_PAIRS)
def test_illegal_transition_raises_conflict(source: str, target: str) -> None:
    """未登记的流转一律拒绝, 错误码与详情都稳定。"""
    with pytest.raises(ConflictError) as exc:
        ensure_transition(source, target)
    assert exc.value.error_code == "conflict"
    assert exc.value.http_status == 409
    assert exc.value.detail == {"from": source, "to": target}


def test_transition_table_covers_every_status() -> None:
    """每个状态都必须在流转表里登记, 否则新增状态会被静默放行或静默拒绝。"""
    assert set(RUN_TRANSITIONS) == set(RunStatus)


def test_terminal_statuses_have_no_exits() -> None:
    """终态没有出口。"""
    for status in TERMINAL_RUN_STATUSES:
        assert RUN_TRANSITIONS[status] == frozenset()


def test_terminal_set_matches_empty_exits() -> None:
    """终态集合与「无出口的状态」是同一份定义, 防止两处漂移。"""
    assert TERMINAL_RUN_STATUSES == frozenset(status for status in RunStatus if not RUN_TRANSITIONS[status])


@pytest.mark.parametrize("status", [item.value for item in RunStatus])
def test_is_terminal(status: str) -> None:
    """is_terminal 与终态集合逐状态一致。"""
    assert is_terminal(status) is (status in TERMINAL_RUN_STATUSES)


def test_ensure_transition_accepts_enum_and_string_alike() -> None:
    """枚举成员与字符串可混用, 数据库读出的字符串无需先转换。"""
    assert ensure_transition(RunStatus.RUNNING, "succeeded") is RunStatus.SUCCEEDED
    assert ensure_transition("running", RunStatus.SUCCEEDED) is RunStatus.SUCCEEDED


def test_ensure_transition_rejects_unknown_value() -> None:
    """未知状态值在归一阶段就失败, 不会走到流转判断。"""
    with pytest.raises(ValueError):
        ensure_transition("running", "exploded")


def test_run_status_values_are_stable() -> None:
    """六个状态值是已入库的线上契约, 改名等于一次数据迁移。"""
    assert [item.value for item in RunStatus] == [
        "pending",
        "running",
        "waiting_approval",
        "succeeded",
        "failed",
        "cancelled",
    ]


def test_waiting_approval_returns_to_running_on_resume() -> None:
    """审批通过后回到 running: 运行状态与审批状态分开的关键路径。"""
    assert ensure_transition(RunStatus.WAITING_APPROVAL, RunStatus.RUNNING) is RunStatus.RUNNING


def test_pending_cannot_jump_to_success() -> None:
    """排队中的运行不能直接成功, 它还没取得执行权。"""
    with pytest.raises(ConflictError):
        ensure_transition(RunStatus.PENDING, RunStatus.SUCCEEDED)
