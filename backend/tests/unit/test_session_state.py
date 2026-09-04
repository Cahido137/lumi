"""会话运行共享状态单元测试"""

import asyncio

import app.core.session_runner.state as state
import pytest
from app.core.session_runner.state import (
    bump_cancel_generation,
    get_cancel_event,
    get_cancel_generation,
    get_session_lock,
    has_pending_runs,
    is_session_running,
    register_active_task,
    register_pending_run,
    request_cancel_session,
    unregister_active_task,
    unregister_pending_run,
)


@pytest.fixture(autouse=True)
def _clean_session_state():
    """测试结束后清空全局状态"""
    yield
    state._session_lock.clear()
    state._cancel_events.clear()
    state._cancel_generations.clear()
    state._active_runs.clear()
    state._active_tasks.clear()
    state._pending_runs.clear()


def test_session_lock_reused_per_session():
    """测试同一个会话获取的是同一个锁对象而不同会话获取不同锁对象"""
    assert get_session_lock("s1") is get_session_lock("s1")
    assert get_session_lock("s1") is not get_session_lock("s2")


def test_cancel_event_reused_per_session():
    """测试同一个会话的取消事件是同一个对象"""
    assert get_cancel_event("s1") is get_cancel_event("s1")
    assert get_cancel_event("s1") is not get_cancel_event("s2")


def test_cancel_generation_bump():
    """取消代际递增"""
    assert get_cancel_generation("g1") == 0
    bump_cancel_generation("g1")
    bump_cancel_generation("g1")
    assert get_cancel_generation("g1") == 2


def test_pending_run_counting():
    """排队计数增减正确, 且多注销一次不会产生负数(幂等)"""
    register_pending_run("p1")
    register_pending_run("p1")
    assert has_pending_runs("p1") is True
    unregister_pending_run("p1")
    assert has_pending_runs("p1") is True
    unregister_pending_run("p1")
    assert has_pending_runs("p1") is False
    unregister_pending_run("p1")
    assert has_pending_runs("p1") is False


async def test_request_cancel_cancels_active_task():
    """有活动任务时请求取消: 返回True、任务被取消、取消事件置位"""
    sid = "c1"
    state._active_runs.add(sid)
    task = asyncio.create_task(asyncio.sleep(30))
    register_active_task(sid, task)
    try:
        assert request_cancel_session(sid) is True
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled() is True
        assert get_cancel_event(sid).is_set() is True
    finally:
        await asyncio.gather(task, return_exceptions=True)  # 回收被取消的任务
        unregister_active_task(sid)


async def test_request_cancel_bumps_generation_for_queued():
    """只有排队任务(还没拿到锁): 取消使代际加一, 排队任务随后自行放弃"""
    register_pending_run("q1")
    gen_before = get_cancel_generation("q1")
    assert request_cancel_session("q1") is True
    assert get_cancel_generation("q1") == gen_before + 1
    unregister_pending_run("q1")


def test_request_cancel_noop_when_idle():
    """完全空闲的会话: 返回False, 没有可取消的东西"""
    assert request_cancel_session("i1") is False


def test_is_session_running_reflects_active_runs():
    """运行状态判断与活动运行集合同步"""
    assert is_session_running("r1") is False
    state._active_runs.add("r1")
    assert is_session_running("r1") is True
    state._active_runs.discard("r1")
    assert is_session_running("r1") is False


def test_is_session_running_isolated_per_session():
    """一个会话运行中不影响其他会话的判断"""
    state._active_runs.add("r2")
    assert is_session_running("r2") is True
    assert is_session_running("r3") is False
