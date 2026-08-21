"""工具执行的 CRUD 操作"""

from sqlalchemy import update, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ToolExecution
from app.schemas.enums import ExecutionStatus


async def create_pending_execution(db: AsyncSession, session_id: str, tool_name: str, tool_input: dict) -> ToolExecution:
    """创建待审批状态的工具执行记录"""
    execution = ToolExecution(
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        status=ExecutionStatus.PENDING.value,
        needs_approval=True
    )
    # 数据落库
    db.add(execution)
    await db.flush()
    return execution

async def finish_execution(db: AsyncSession, execution_id: str, status: ExecutionStatus | str, output: str) -> None:
    """执行完成更新状态"""
    if isinstance(status, ExecutionStatus):
        status = status.value
    stmt = update(ToolExecution).where(ToolExecution.id == execution_id).values(status=status, tool_output=output, finished_at=func.now())
    await db.execute(stmt)
    await db.flush()

async def get_pending_execution(db: AsyncSession, session_id: str):
    """查看会话中最早的一条待审批记录"""
    stmt = (
        select(ToolExecution)
        .where(
            ToolExecution.session_id == session_id, 
            ToolExecution.status == ExecutionStatus.PENDING.value
        ).order_by(ToolExecution.started_at.asc())
    )
    result = await db.execute(stmt)
    return result.scalars().first()