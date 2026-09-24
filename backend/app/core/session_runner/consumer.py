"""同进程的运行命令消费者。"""

import logging

from app.crud import run_commands as run_commands_crud
from app.crud import runs as runs_crud
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


async def claim_execution(run_id: str, command_id: str) -> bool:
    """在一个短事务里领取运行执行权与指定待执行命令。

    Args:
        run_id: 运行记录ID。
        command_id: 待领取的命令ID。

    Returns:
        bool: 是否领取成功。
    """
    async with SessionLocal() as db:
        if not await runs_crud.mark_run_started(db, run_id):
            await db.rollback()
            return False
        if not await run_commands_crud.claim_command(db, command_id):
            logger.warning("命令已被其他执行方领取: run_id=%s, command=%s", run_id, command_id)
            await db.rollback()
            return False
        await db.commit()
        return True
