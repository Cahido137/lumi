"""工具执行的 CRUD 操作"""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ToolExecution


async def create_pending_execution(db: AsyncSession, session_id: str, tool_name: str, tool_input: dict) -> ToolExecution:
    """创建待审批状态的工具执行记录"""
    execution = ToolExecution(
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        status="pending",
        needs_approval=True
    )
    # 数据落库
    db.add(execution)
    await db.flush()
    return execution

async def finish_execution(db: AsyncSession, execution_id: str, status: str, output: str) -> None:
    """执行完成更新状态"""
    stmt = update(ToolExecution).where(ToolExecution.id == execution_id).values(status=status, tool_output=output)
    await db.execute(stmt)
    await db.flush()