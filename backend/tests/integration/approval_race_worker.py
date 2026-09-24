"""跨进程审批竞争的子进程执行体。"""

import argparse
import asyncio
import os
import sys


async def guard_database(expected: str) -> None:
    """连接后核对实际库名, 与期望不符立即退出。

    Args:
        expected: 父进程传入的期望库名。

    Raises:
        SystemExit: 实际连接的库不是期望的库。
    """
    from app.db.session import SessionLocal
    from sqlalchemy import text

    async with SessionLocal() as db:
        actual = (await db.execute(text("SELECT current_database()"))).scalar_one()
    if actual != expected:
        print(f"REFUSED {actual}", flush=True)
        raise SystemExit(2)


async def decide(approval_id: str, status: str, scope: str, hold: bool) -> None:
    """执行一次条件审批决定。

    Args:
        approval_id: 审批单ID。
        status: 审批决定。
        scope: 审批授权范围。
        hold: 为 True 时只 flush 不提交, 等父进程的 COMMIT 或 ROLLBACK 指令。
    """
    from app.crud import approvals as approvals_crud
    from app.db.session import SessionLocal

    async with SessionLocal() as db:
        decided = await approvals_crud.update_approval(db, approval_id, status, scope)
        print(f"EXECUTED {decided}", flush=True)
        if not hold:
            await db.commit()
            print("COMMITTED", flush=True)
            return
        # 事务保持打开, 因此本进程持有该审批行的写锁
        instruction = sys.stdin.readline().strip()
        if instruction == "COMMIT":
            await db.commit()
            print("COMMITTED", flush=True)
        else:
            await db.rollback()
            print("ROLLED_BACK", flush=True)


async def queue(approval_id: str, thread_id: str, status: str, scope: str) -> None:
    """执行一次真实的审批决定短事务: 决定 + 恢复命令 + 运行退回排队。

    Args:
        approval_id: 审批单ID。
        thread_id: 审批单对应的检查点线程ID。
        status: 审批决定。
        scope: 审批授权范围。
    """
    from app.core.session_runner import runner
    from app.schemas.enums import ApprovalScope, ApprovalStatus

    queued = await runner._decide_and_queue(approval_id, thread_id, ApprovalStatus(status), ApprovalScope(scope))
    if queued is None:
        print("QUEUED NONE", flush=True)
    else:
        print(f"QUEUED {queued[0]} {queued[1]}", flush=True)


async def main() -> None:
    """解析参数, 完成握手后执行一次竞争动作。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--mode", required=True, choices=("decide", "queue"))
    parser.add_argument("--approval-id", required=True)
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--status", required=True)
    parser.add_argument("--scope", default="one_time")
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args()

    # 目标库由父进程通过环境变量固定, 缺失时拒绝启动
    if not os.environ.get("DATABASE_URL"):
        print("REFUSED no-database-url", flush=True)
        raise SystemExit(2)

    await guard_database(args.database)
    print("READY", flush=True)
    sys.stdin.readline()  # 等父进程放行, 使两个进程的竞争窗口尽量重合

    if args.mode == "decide":
        await decide(args.approval_id, args.status, args.scope, args.hold)
    else:
        await queue(args.approval_id, args.thread_id, args.status, args.scope)


if __name__ == "__main__":
    asyncio.run(main())
