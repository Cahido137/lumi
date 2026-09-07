"""工具执行的 CRUD 操作。

需要审批的工具调用先由 create_pending_execution 在执行前落库, 执行结束后由 finish_execution 更新。
无需审批的工具调用由 create_finished_execution 在执行后一次性写入。

Note:
    本模块的所有写操作均只 flush 不 commit, 事务边界由调用方决定。
"""

from datetime import datetime

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ToolExecution
from app.schemas.enums import ExecutionStatus


async def create_pending_execution(
    db: AsyncSession, session_id: str, tool_name: str, tool_input: dict, tool_call_id: str | None = None
) -> ToolExecution:
    """创建待审批状态的工具执行记录。

    Args:
        session_id: 所属会话ID。
        tool_name: 工具名称。
        tool_input: 工具入参字典。
        tool_call_id: 工具标识ID, 可空。

    Returns:
        ToolExecution: 成功创建的工具执行记录。
    """
    execution = ToolExecution(
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_call_id=tool_call_id,
        status=ExecutionStatus.PENDING.value,
        needs_approval=True,
    )
    # 数据落库
    db.add(execution)
    await db.flush()
    return execution


async def finish_execution(db: AsyncSession, execution_id: str, status: ExecutionStatus | str, output: str) -> None:
    """执行完成更新工具执行记录状态。

    Args:
        execution_id: 工具执行记录ID。
        status: 工具执行状态 ExecutionStatus 枚举或其字符串字面量。
        output: 工具输出文本。
    """
    if isinstance(status, ExecutionStatus):
        status = status.value
    stmt = (
        update(ToolExecution)
        .where(ToolExecution.id == execution_id)
        .values(status=status, tool_output=output, finished_at=text("clock_timestamp()"))
    )
    await db.execute(stmt)
    await db.flush()


async def create_finished_execution(
    db: AsyncSession,
    session_id: str,
    tool_name: str,
    tool_call_id: str,
    tool_input: dict,
    status: ExecutionStatus | str,
    tool_output: str | None = None,
):
    """一次性记录一条完整的工具执行记录。

    Args:
        session_id: 所属会话ID。
        tool_name: 工具名称。
        tool_call_id: 发起本次调用的标识ID。
        tool_input: 工具入参字典。
        status: 工具执行结果状态, 接受 ExecutionStatus 枚举或其字符串字面量。
        tool_output: 工具输出, 可空。

    Returns:
        ToolExecution: 经过 flush 的执行记录对象。

    Note:
        本函数只可用于无需审批的工具一次性落库执行结果, 因为此函数将 needs_approval 参数固定为 False。
    """
    status_value = status.value if isinstance(status, ExecutionStatus) else status
    execution = ToolExecution(
        session_id=session_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_input=tool_input or {},
        status=status_value,
        tool_output=tool_output,
        needs_approval=False,
        finished_at=text("clock_timestamp()"),
    )
    db.add(execution)
    await db.flush()
    return execution


async def get_pending_execution(db: AsyncSession, session_id: str):
    """查询会话中最早的一条待审批记录。

    Args:
        session_id: 所属会话ID。

    Returns:
        如果存在最早的待审批记录, 返回其 ToolExecution 对象; 如果不存在, 返回 None。
    """
    stmt = (
        select(ToolExecution)
        .where(ToolExecution.session_id == session_id, ToolExecution.status == ExecutionStatus.PENDING.value)
        .order_by(ToolExecution.started_at.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().first()


async def get_pending_execution_by_call_id(
    db: AsyncSession, session_id: str, tool_call_id: str | None
) -> ToolExecution | None:
    """按工具标识ID查询执行记录。

    Args:
        session_id: 所属会话ID。
        tool_call_id: 指定工具标识ID。

    Returns:
        如果指定会话下存在指定工具标识ID的工具调用, 返回其 ToolExecution 对象。
        如果传入的指定工具标识ID为空, 或在当前会话下没有指定工具标识ID, 返回 None。
    """
    if not tool_call_id:
        return None
    stmt = (
        select(ToolExecution)
        .where(
            ToolExecution.session_id == session_id,
            ToolExecution.status == ExecutionStatus.PENDING.value,
            ToolExecution.tool_call_id == tool_call_id,
        )
        .order_by(ToolExecution.started_at.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().first()


async def delete_execution_after(db: AsyncSession, session_id: str, started_at: datetime) -> None:
    """删除指定会话中某个时间点之后开始的所有工具执行记录。

    Args:
        session_id: 所属会话ID。
        started_at: 指定的时间戳。
    """
    stmt = delete(ToolExecution).where(ToolExecution.session_id == session_id, ToolExecution.started_at > started_at)
    await db.execute(stmt)
    await db.flush()
