"""审批单的 CRUD 操作"""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval


async def create_approval(db: AsyncSession, session_id: str, thread_id: str, tool_execution_id: str) -> Approval:
    """创建审批单"""
    approval = Approval(
        session_id=session_id,
        thread_id=thread_id,
        tool_execution_id=tool_execution_id,
        status="pending",
        scope="one_time"
    )
    # 落库
    db.add(approval)
    await db.flush()
    return approval

async def get_approval_by_id(db: AsyncSession, approval_id: str) -> Approval | None:
    """按审批单ID查询审批单"""
    return await db.get(Approval, approval_id)

async def update_approval(db: AsyncSession, approval_id: str, status: str, scope: str) -> None:
    """更新审批单"""
    stmt = update(Approval).where(Approval.id == approval_id).values(status=status, scope=scope)
    await db.execute(stmt)
    await db.flush()