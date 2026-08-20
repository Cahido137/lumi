"""审批单的 CRUD 操作"""

from sqlalchemy import update, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, ToolExecution
from app.utils.dict import normalize_dict


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
    stmt = update(Approval).where(Approval.id == approval_id).values(status=status, scope=scope, decided_at=func.now())
    await db.execute(stmt)
    await db.flush()

async def get_session_grants(db: AsyncSession, session_id: str) -> dict:
    """查询指定会话下的工具授权"""
    # 定义授权结构
    grants: dict = {"tool": [], "command": {}}
    # 联表查询已批准的工具调用查看授权情况
    stmt = (
        select(Approval.scope, ToolExecution.tool_name, ToolExecution.tool_input)
        .join(ToolExecution, Approval.tool_execution_id == ToolExecution.id)
        .where(
            Approval.session_id == session_id,  # 指定会话下的
            Approval.status == "approved",  # 已授权的
            Approval.scope.in_(["tool", "command"])  # 查询授权
        )
    )
    result = await db.execute(stmt)
    for scope, tool_name, tool_input in result.all():
        # 如果为整个工具完全授权执行
        if scope == "tool":
            if tool_name not in grants["tool"]:
                grants["tool"].append(tool_name)  # 加入工具授权
        # 如果为指定命令授权
        else:
            key = normalize_dict(tool_input)
            commands = grants["command"].setdefault(tool_name, [])
            if key not in commands:
                commands.append(key)  # 加入命令授权
    return grants

async def has_pending_approval(db: AsyncSession, session_id: str) -> bool:
    """检查当前会话是否还有未审批的审批单"""
    stmt = select(Approval.id).where(Approval.session_id == session_id, Approval.status == "pending").limit(1)
    result = await db.execute(stmt)
    return result.first() is not None