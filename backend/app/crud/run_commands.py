"""运行命令的 CRUD 操作。

Note:
    本模块的所有写操作均只 flush 不 commit, 事务边界由调用方决定。
"""

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.run_state import COMMAND_TRANSITIONS
from app.db.models import RunCommand
from app.schemas.enums import RunCommandKind, RunCommandStatus

ACTIVE_COMMAND_STATUSES = (RunCommandStatus.PENDING, RunCommandStatus.CLAIMED)
"""表示未完成的命令状态, 同一条运行最多只能有一条未完成的命令。"""


def _allowed_sources(target: RunCommandStatus) -> list[str]:
    """列出允许流转到指定目标状态的全部源状态。

    Args:
        target: 目标状态。

    Returns:
        list[str]: 源状态的字符串字面量值列表。
    """
    return [source.value for source, targets in COMMAND_TRANSITIONS.items() if target in targets]


async def create_command(
    db: AsyncSession,
    run_id: str,
    kind: RunCommandKind | str,
    *,
    approval_id: str | None = None,
    payload: dict | None = None,
) -> RunCommand:
    """登记一条待领取的运行命令。

    Args:
        run_id: 所属运行ID。
        kind: 命令种类 RunCommandKind 枚举类型或其字符串字面量。
        approval_id: resume 种类时传入命令对应审批单ID, start 种类传递 None。
        payload: 不含身份与授权的命令载荷。

    Returns:
        RunCommand: 成功创建的命令对象。
    """
    command = RunCommand(
        run_id=run_id,
        kind=kind.value if isinstance(kind, RunCommandKind) else kind,
        approval_id=approval_id,
        payload=payload,
        status=RunCommandStatus.PENDING.value,
    )
    db.add(command)
    await db.flush()
    return command


async def get_command_by_id(db: AsyncSession, command_id: str) -> RunCommand | None:
    """按命令主键查询命令。

    Args:
        command_id: 命令主键ID。

    Returns:
        返回指定的 RunCommand 对象, 不存在返回 None。
    """
    return await db.get(RunCommand, command_id)


async def get_active_command(db: AsyncSession, run_id: str) -> RunCommand | None:
    """查询某条运行尚未完成的命令。

    Args:
        run_id: 运行记录ID。

    Returns:
        返回处于未完成状态的命令 (pending, claimed), 不存在返回 None。
    """
    stmt = select(RunCommand).where(RunCommand.run_id == run_id, RunCommand.status.in_(ACTIVE_COMMAND_STATUSES))
    result = await db.execute(stmt)
    return result.scalars().first()


async def list_commands(db: AsyncSession, run_id: str) -> list[RunCommand]:
    """列出指定运行的全部命令。

    Args:
        run_id: 运行记录ID。

    Returns:
        list[RunCommand]: 按登记时间正序排列的命令列表。
    """
    stmt = select(RunCommand).where(RunCommand.run_id == run_id).order_by(RunCommand.created_at, RunCommand.id)
    result = await db.execute(stmt)
    return list(result.scalars())


async def _advance(db: AsyncSession, command_id: str, target: RunCommandStatus, **values) -> bool:
    """把一条命令推进到目标状态。

    Args:
        command_id: 命令ID。
        target: 目标状态。
        **values: 随流转一并写入到列。

    Returns:
        bool: 成功写入返回 True, 命令不存在或者流转非法返回 False。
    """
    stmt = (
        update(RunCommand)
        .where(RunCommand.id == command_id, RunCommand.status.in_(_allowed_sources(target)))
        .values(status=target.value, **values)
        .returning(RunCommand.id)
    )
    result = await db.execute(stmt)
    advanced = result.scalar_one_or_none() is not None
    await db.flush()
    return advanced


async def claim_command(db: AsyncSession, command_id: str) -> bool:
    """领取一条待执行的命令。

    Args:
        command_id: 命令ID。

    Returns:
        成功领取返回 True, 命令不存在或已被领取返回 False。
    """
    return await _advance(
        db,
        command_id,
        RunCommandStatus.CLAIMED,
        claimed_epoch=text("claimed_epoch + 1"),
        delivery_attempt=text("delivery_attempt + 1"),
    )


async def advance_command(db: AsyncSession, command_id: str, target: RunCommandStatus | str) -> bool:
    """把命令推进到指定的状态。

    Args:
        command_id: 命令ID。
        target: 目标状态 RunCommandStatus 枚举类型或其字符串字面量。

    Returns:
        bool: 是否推进成功。
    """
    return await _advance(db, command_id, RunCommandStatus(target))


async def cancel_pending_commands(db: AsyncSession, run_id: str) -> int:
    """把指定运行尚未领取的所有命令置为 cancelled。

    Args:
        run_id: 运行记录ID。

    Returns:
        int: 实际取消的命令条数。
    """
    stmt = (
        update(RunCommand)
        .where(RunCommand.run_id == run_id, RunCommand.status == RunCommandStatus.PENDING.value)
        .values(status=RunCommandStatus.CANCELLED.value)
        .returning(RunCommand.id)
    )
    result = await db.execute(stmt)
    count = len(result.scalars().all())
    await db.flush()
    return count
