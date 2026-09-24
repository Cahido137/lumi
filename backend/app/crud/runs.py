"""运行记录表的 CRUD 操作。

Note:
    本模块的所有写操作均只 flush 不 commit, 事务边界由调用方决定。
    状态推进把当前状态允许该流转写进 UPDATE 的 WHERE 条件, 由数据库侧原子判定,
    因此返回 False 只表示没有写入(记录不存在或流转非法), 本模块不抛业务异常。
"""

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.run_state import RUN_TRANSITIONS, TERMINAL_RUN_STATUSES
from app.db.models import Run
from app.schemas.enums import RunStatus


def _allowed_sources(target: RunStatus) -> list[str]:
    """列出允许路由到指定目标状态的全部源状态。

    Args:
        target: 目标状态。

    Returns:
        list[str]: 源状态的字符串字面量值。
    """
    return [source.value for source, targets in RUN_TRANSITIONS.items() if target in targets]


async def create_run(
    db: AsyncSession,
    session_id: str,
    thread_id: str,
    *,
    input_message_id: str | None = None,
    attempt: int = 1,
    request_id: str | None = None,
    request_fingerprint: str | None = None,
) -> Run:
    """登记一次新的运行。

    Args:
        session_id: 所属会话ID。
        thread_id: 本轮运行使用的 LangGraph 线程ID。
        input_message_id: 触发本轮运行的用户消息ID。
        attempt: 同一条消息的第几次尝试, 默认值为 1。

    Returns:
        Run: 成功创建的状态为 pending 的运行记录对象。
    """
    run = Run(
        session_id=session_id,
        thread_id=thread_id,
        status=RunStatus.PENDING.value,
        input_message_id=input_message_id,
        attempt=attempt,
        request_id=request_id,
        request_fingerprint=request_fingerprint,
    )
    db.add(run)
    await db.flush()
    return run


async def get_run_by_id(db: AsyncSession, run_id: str) -> Run | None:
    """按主键ID查询运行记录。

    Args:
        run_id: 运行记录的主键。

    Returns:
        返回指定的 Run 对象, 不存在返回 None。
    """
    return await db.get(Run, run_id)


async def get_run_by_thread_id(db: AsyncSession, thread_id: str) -> Run | None:
    """按 LangGraph 的线程ID查询运行记录。

    Args:
        thread_id: LangGraph 的线程ID。

    Returns:
        返回指定的 Run 对象, 不存在返回 None。
    """
    stmt = select(Run).where(Run.thread_id == thread_id).order_by(Run.created_at.desc()).limit(1)
    result = await db.execute(stmt)
    return result.scalars().first()


async def get_run_by_request_id(db: AsyncSession, request_id: str) -> Run | None:
    """按幂等键查询运行记录。

    Args:
        request_id: 幂等键。

    Returns:
        返回指定的 Run 对象, 不存在返回 None。
    """
    stmt = select(Run).where(Run.request_id == request_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_active_run(db: AsyncSession, session_id: str) -> Run | None:
    """查询指定会话当前尚未结束的运行。

    Args:
        session_id: 会话ID。

    Returns:
        最新一条非终态运行记录, 没有则返回 None。
    """
    stmt = (
        select(Run)
        .where(Run.session_id == session_id, Run.status.not_in([item.value for item in TERMINAL_RUN_STATUSES]))
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalars().first()


async def list_runs_for_session(db: AsyncSession, session_id: str, skip: int = 0, limit: int = 20) -> list[Run]:
    """按时间倒序分页查询指定会话的运行历史记录。

    Args:
        session_id: 会话ID。
        skip: 跳过的条数。
        limit: 单页条数, 默认为 20。

    Returns:
        list[Run]: 运行记录列表, 时间倒序。
    """
    stmt = (
        select(Run)
        .where(Run.session_id == session_id)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _advance(db: AsyncSession, run_id: str, target: RunStatus, **values) -> bool:
    """把一条运行记录推进到目标状态。

    Args:
        run_id: 运行记录ID。
        target: 目标状态。
        **values: 随本次流转一并写入的其余列。

    Returns:
        bool: 写入成功返回 True, 记录不存在或非法流转返回 False。

    Note:
        流转条件写在 WHERE 里由数据库原子判定, 因此并发调用最多只有一个成功。
    """
    stmt = (
        update(Run)
        .where(Run.id == run_id, Run.status.in_(_allowed_sources(target)))
        .values(status=target.value, **values)
        .returning(Run.id)
    )
    result = await db.execute(stmt)
    updated = result.scalar_one_or_none() is not None
    await db.flush()
    return updated


async def mark_run_started(db: AsyncSession, run_id: str) -> bool:
    """把运行推进到 running。

    Args:
        run_id: 运行记录ID。

    Returns:
        bool: 是否成功流转。
    """
    # 使用 COALESCE 写入，防止审批恢复重新开始运行时覆盖 started_at 的值
    return await _advance(db, run_id, RunStatus.RUNNING, started_at=text("COALESCE(started_at, clock_timestamp())"))


async def mark_run_queued(db: AsyncSession, run_id: str) -> bool:
    """把运行退回 pending, 标识恢复命令已经排队等待领取。

    Args:
        run_id: 运行记录ID。

    Returns:
        bool: 是否成功流转。
    """
    return await _advance(db, run_id, RunStatus.PENDING)


async def mark_run_waiting_approval(db: AsyncSession, run_id: str) -> bool:
    """把运行推进到 waiting_approval。

    Args:
        run_id: 运行记录ID。

    Returns:
        bool: 是否成功流转。

    Note:
        等待审批不是结束, 因此本函数是五个 mark_ 里唯一不写 finished_at 的。
    """
    # 等待审批状态为正常状态，不需要传递错误码
    return await _advance(db, run_id, RunStatus.WAITING_APPROVAL)


async def mark_run_succeeded(db: AsyncSession, run_id: str, *, output_message_id: str | None = None) -> bool:
    """把运行推进到 succeeded。

    Args:
        run_id: 运行记录ID。

    Returns:
        bool: 是否成功流转。
    """
    # 成功运行清空错误码
    return await _advance(
        db,
        run_id,
        RunStatus.SUCCEEDED,
        error_code=None,
        finished_at=text("clock_timestamp()"),
        output_message_id=output_message_id,
    )


async def mark_run_failed(db: AsyncSession, run_id: str, *, error_code: str) -> bool:
    """把运行推进到 failed 并记录稳定错误码。

    Args:
        run_id: 运行记录ID。
        error_code: 稳定错误码。

    Returns:
        bool: 是否成功流转。
    """
    return await _advance(db, run_id, RunStatus.FAILED, error_code=error_code, finished_at=text("clock_timestamp()"))


async def mark_run_cancelled(db: AsyncSession, run_id: str) -> bool:
    """把运行推进到 cancelled。

    Args:
        run_id: 运行记录ID。

    Returns:
        bool: 是否成功流转。
    """
    # 取消运行清空错误码
    return await _advance(db, run_id, RunStatus.CANCELLED, error_code=None, finished_at=text("clock_timestamp()"))


async def request_run_cancel(db: AsyncSession, run_id: str) -> bool:
    """登记一次打断请求的时刻, 不会改变运行状态。

    Args:
        run_id: 运行记录ID。

    Returns:
        bool: 首次登记成功返回 True, 运行不存在、运行结束或已登记过返回 False。
    """
    stmt = (
        update(Run)
        .where(
            Run.id == run_id,
            Run.cancel_requested_at.is_(None),
            Run.status.not_in([item.value for item in TERMINAL_RUN_STATUSES]),
        )
        .values(cancel_requested_at=text("clock_timestamp()"))
        .returning(Run.id)
    )
    result = await db.execute(stmt)
    updated = result.scalar_one_or_none() is not None
    await db.flush()
    return updated


async def next_attempt(db: AsyncSession, session_id: str, input_message_id: str) -> int:
    """计算同一条输入消息的下一次尝试序号。

    Args:
        session_id: 会话ID。
        input_message_id: 输入消息ID。

    Returns:
        int: 下一个 attempt 值, 从未尝试过为 1。
    """
    stmt = select(func.coalesce(func.max(Run.attempt), 0)).where(
        Run.session_id == session_id, Run.input_message_id == input_message_id
    )
    result = await db.execute(stmt)
    return int(result.scalar_one()) + 1
