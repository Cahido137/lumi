"""审批单的 CRUD 操作。

Note:
    本模块的所有写操作均只 flush 不 commit, 事务边界由调用方决定。
"""

from datetime import datetime

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.grants import Grants, extract_grant_key
from app.db.models import Approval, ToolExecution
from app.schemas.enums import ApprovalScope, ApprovalStatus


async def create_approval(db: AsyncSession, session_id: str, thread_id: str, tool_execution_id: str) -> Approval:
    """创建一个新审批单。

    Args:
        session_id: 会话ID。
        thread_id: 检查点线程ID。
        tool_execution_id: 被审批的工具执行记录。

    Returns:
        Approval: 创建出的审批单对象。

    Note:
        创建出的审批单状态为 pending, 也就是还未审批状态。
    """
    approval = Approval(
        session_id=session_id,
        thread_id=thread_id,
        tool_execution_id=tool_execution_id,
        status=ApprovalStatus.PENDING.value,
        scope=ApprovalScope.ONE_TIME.value,
    )
    # 落库
    db.add(approval)
    await db.flush()
    return approval


async def get_approval_by_id(db: AsyncSession, approval_id: str) -> Approval | None:
    """按审批单主键ID查询审批单。

    Args:
        approval_id: 审批单主键ID。

    Returns:
        如果存在指定主键ID, 返回指定的 Approval 对象; 如果不存在指定主键ID, 返回 None。
    """
    return await db.get(Approval, approval_id)


async def get_approval_by_execution_id(db: AsyncSession, tool_execution_id: str) -> Approval | None:
    """按工具执行记录ID查询审批单。

    Args:
        tool_execution_id: 工具执行记录ID。

    Returns:
        如果存在指定审批单, 返回指定的 Approval 对象; 如果不存在, 返回 None。
    """
    stmt = select(Approval).where(Approval.tool_execution_id == tool_execution_id)
    result = await db.execute(stmt)
    return result.scalars().first()


async def update_approval(
    db: AsyncSession, approval_id: str, status: ApprovalStatus | str, scope: ApprovalScope | str
) -> None:
    """更新审批单信息。

    Args:
        approval_id: 审批单ID。
        status: 审批状态 ApprovalStatus 枚举类型或其字符串字面量。
        scope: 审批授权范围 ApprovalScope 枚举类型或其字符串字面量。
    """
    status_value = status.value if isinstance(status, ApprovalStatus) else status
    scope_value = scope.value if isinstance(scope, ApprovalScope) else scope
    stmt = (
        update(Approval)
        .where(Approval.id == approval_id)
        .values(status=status_value, scope=scope_value, decided_at=text("clock_timestamp()"))
    )
    await db.execute(stmt)
    await db.flush()


async def get_session_grants(db: AsyncSession, session_id: str) -> Grants:
    """汇总指定会话当前生效的工具授权快照。

    Args:
        session_id: 会话ID。

    Returns:
        Grants: 授权快照。无授权时各字段为空。

    Note:
        one_time 授权范围不计入授权快照。
    """
    grants = Grants()
    # 联表查询已批准的工具调用查看授权情况
    stmt = (
        select(Approval.scope, ToolExecution.tool_name, ToolExecution.tool_input)
        .join(ToolExecution, Approval.tool_execution_id == ToolExecution.id)
        .where(
            Approval.session_id == session_id,  # 指定会话下的
            Approval.status == ApprovalStatus.APPROVED.value,  # 已授权的
            Approval.scope.in_([ApprovalScope.TOOL.value, ApprovalScope.COMMAND.value]),  # 查询授权
        )
    )
    result = await db.execute(stmt)
    for scope, tool_name, tool_input in result.all():
        # 如果为整个工具完全授权执行
        if scope == ApprovalScope.TOOL:
            if tool_name not in grants.tool:
                grants.tool.append(tool_name)  # 加入工具授权
        # 如果为指定命令授权
        else:
            key = extract_grant_key(tool_name, tool_input)
            commands = grants.command.setdefault(tool_name, [])
            if key not in commands:
                commands.append(key)  # 加入命令授权
    return grants


async def has_pending_approval(db: AsyncSession, session_id: str) -> bool:
    """检查当前会话是否还有未审批的审批单。

    Args:
        session_id: 会话ID。

    Returns:
        bool: 当前会话是否有还没有审批的审批单。
    """
    stmt = (
        select(Approval.id)
        .where(Approval.session_id == session_id, Approval.status == ApprovalStatus.PENDING.value)
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.first() is not None


async def revert_approval(db: AsyncSession, approval_id: str) -> None:
    """回滚审批单状态回 pending, 并清空授权范围与决定时间。

    Args:
        approval_id: 审批单ID。

    Note:
        用于审批通过后但是恢复图失败的场景。
        回滚的审批单将会作废先前的决定, 允许重新审批。
    """
    stmt = (
        update(Approval)
        .where(Approval.id == approval_id)
        .values(status=ApprovalStatus.PENDING.value, scope=ApprovalScope.ONE_TIME.value, decided_at=None)
    )
    await db.execute(stmt)
    await db.flush()


async def delete_approval_after(db: AsyncSession, session_id: str, created_at: datetime) -> None:
    """删除指定会话中某个时间点之后创建的所有审批单。

    Args:
        session_id: 会话ID。
        created_at: 指定的时间戳。

    Note:
        本函数是按审批单创建时间是否在指定时间戳之后判断是否需要删除。
    """
    stmt = delete(Approval).where(Approval.session_id == session_id, Approval.created_at > created_at)
    await db.execute(stmt)
    await db.flush()
