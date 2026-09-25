"""集成测试: 两个独立 OS 进程竞争同一张审批单(真库, 不跑图)。

Note:
    14.2 要求 3.5a 至少有两个独立进程或明确隔离的运行时实例。
    本文件的两个进程各自持有独立的会话锁与独立的数据库连接, 因此结论不依赖进程内互斥。
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from app.core.session_runner.consumer import claim_execution
from app.crud import approvals as approvals_crud
from app.crud import run_commands as run_commands_crud
from app.crud import runs as runs_crud
from app.crud import sessions as sessions_crud
from app.crud import tool_executions as tool_executions_crud
from app.crud import users as users_crud
from app.db.models import Approval, Run
from app.db.session import SessionLocal
from app.schemas.enums import ApprovalScope, ApprovalStatus, RunCommandKind, RunCommandStatus, RunStatus
from sqlalchemy import text

BACKEND_DIR = Path(__file__).resolve().parents[2]
"""仓库的 backend 目录, 作为子进程的工作目录与 PYTHONPATH。"""

WORKER = Path(__file__).with_name("approval_race_worker.py")
"""子进程执行体, 文件名不带 test_ 前缀所以不被 pytest 收集。"""

HANDSHAKE_TIMEOUT = 30.0
"""单次握手或回执的超时秒数。"""

BLOCKED_TIMEOUT = 10.0
"""等待子进程真的阻塞在行锁上的超时秒数。"""

TERMINATE_TIMEOUT = 5.0
"""终止子进程的超时秒数。"""


async def current_database() -> str:
    """取当前连接实际访问的库名, 用于传给子进程做二次校验"""
    async with SessionLocal() as db:
        return (await db.execute(text("SELECT current_database()"))).scalar_one()


async def seed_pending_approval(username: str, thread_id: str) -> tuple[str, str]:
    """造一条等待审批的运行与它名下的待决审批, 返回 (运行ID, 审批单ID)"""
    async with SessionLocal() as db:
        user = await users_crud.create_user(db, username, "x")
        await db.commit()
        session = await sessions_crud.create_session(db, "跨进程会话", user.id)
        await db.commit()
        session_id = session.id
    async with SessionLocal() as db:
        run = await runs_crud.create_run(db, session_id, thread_id)
        assert await runs_crud.mark_run_started(db, run.id)
        assert await runs_crud.mark_run_waiting_approval(db, run.id)
        execution = await tool_executions_crud.create_pending_execution(
            db, session_id, "run_shell", {"command": "ls"}, "call-race", run_id=run.id
        )
        approval = await approvals_crud.create_approval(db, session_id, run.id, thread_id, execution.id)
        await db.commit()
        return run.id, approval.id


async def spawn_worker(
    database: str, mode: str, approval_id: str, *, thread_id: str = "", status: str = "approved", hold: bool = False
):
    """启动一个子进程并完成 READY 握手, 返回进程句柄"""
    command = [
        sys.executable,
        str(WORKER),
        "--database",
        database,
        "--mode",
        mode,
        "--approval-id",
        approval_id,
        "--status",
        status,
    ]
    if thread_id:
        command += ["--thread-id", thread_id]
    if hold:
        command.append("--hold")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(BACKEND_DIR),
        env={**os.environ, "PYTHONPATH": str(BACKEND_DIR)},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    ready = await asyncio.wait_for(process.stdout.readline(), HANDSHAKE_TIMEOUT)
    if ready.decode().strip() != "READY":
        stderr = (await process.stderr.read()).decode()
        pytest.fail(f"子进程握手失败: stdout={ready!r}\nstderr={stderr}")
    return process


async def release(process) -> None:
    """给子进程发 GO, 放行它进入竞争"""
    process.stdin.write(b"GO\n")
    await process.stdin.drain()


async def read_receipt(process) -> str:
    """读一行子进程回执"""
    line = await asyncio.wait_for(process.stdout.readline(), HANDSHAKE_TIMEOUT)
    assert line, "子进程在给出回执前就退出了"
    return line.decode().strip()


async def instruct(process, instruction: str) -> None:
    """给处于 hold 模式的子进程发一条事务指令"""
    process.stdin.write(instruction.encode() + b"\n")
    await process.stdin.drain()


async def wait_until_blocked_on_approval_lock() -> bool:
    """有界轮询, 直到出现一个因行锁等待而阻塞的审批更新语句"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BLOCKED_TIMEOUT
    statement = text(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() AND wait_event_type = 'Lock' AND query ILIKE '%UPDATE approvals%'"
    )
    while loop.time() < deadline:
        async with SessionLocal() as db:
            if (await db.execute(statement)).scalar_one() > 0:
                return True
        await asyncio.sleep(0.05)
    return False


async def terminate(process) -> None:
    """收尾子进程: 关管道、终止, 超时后强杀"""
    if process is None or process.returncode is not None:
        return
    try:
        if process.stdin is not None:
            process.stdin.close()
        process.terminate()
        await asyncio.wait_for(process.wait(), TERMINATE_TIMEOUT)
    except (ProcessLookupError, TimeoutError):
        process.kill()
        await process.wait()


async def load_approval(approval_id: str) -> Approval:
    """取审批单"""
    async with SessionLocal() as db:
        approval = await db.get(Approval, approval_id)
    assert approval is not None
    return approval


async def load_run(run_id: str) -> Run:
    """取运行记录"""
    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
    assert run is not None
    return run


async def test_two_processes_deciding_same_approval_yields_single_winner():
    """T05 跨进程: A 持锁未提交时 B 被挡住, A 提交后 B 的条件更新命中零行"""
    database = await current_database()
    run_id, approval_id = await seed_pending_approval("race_t05", "race:t05")
    winner = await spawn_worker(database, "decide", approval_id, status="approved", hold=True)
    loser = None
    try:
        # A 先执行条件更新, 拿到该审批行的写锁但不提交
        await release(winner)
        assert await read_receipt(winner) == "EXECUTED True"

        # B 用相反的决定进入, 必然阻塞在 A 持有的行锁上
        loser = await spawn_worker(database, "decide", approval_id, status="rejected", hold=True)
        await release(loser)
        assert await wait_until_blocked_on_approval_lock(), "B 没有阻塞在行锁上, 竞争窗口没有真正重叠"

        # A 提交后 B 被唤醒, 用新行版本重新求值 WHERE, 命中零行
        await instruct(winner, "COMMIT")
        assert await read_receipt(winner) == "COMMITTED"
        assert await read_receipt(loser) == "EXECUTED False"
        await instruct(loser, "COMMIT")
        assert await read_receipt(loser) == "COMMITTED"

        approval = await load_approval(approval_id)
        assert approval.status == ApprovalStatus.APPROVED.value
        assert approval.scope == ApprovalScope.ONE_TIME.value
        assert approval.decided_at is not None
        # 落败进程没有碰过运行状态
        assert (await load_run(run_id)).status == RunStatus.WAITING_APPROVAL.value
    finally:
        await terminate(loser)
        await terminate(winner)


async def test_two_processes_queueing_resume_creates_single_command():
    """T04 跨进程: 两个进程同时批准, 只有一次有效决定与一条恢复命令"""
    database = await current_database()
    run_id, approval_id = await seed_pending_approval("race_t04", "race:t04")
    first = await spawn_worker(database, "queue", approval_id, thread_id="race:t04", status="approved")
    second = await spawn_worker(database, "queue", approval_id, thread_id="race:t04", status="approved")
    try:
        # 两个进程都已连库并停在 GO 之前, 再背靠背放行, 把竞争窗口压到最小
        await release(first)
        await release(second)
        receipts = await asyncio.gather(read_receipt(first), read_receipt(second))

        winners = [item for item in receipts if item != "QUEUED NONE"]
        assert len(winners) == 1, f"应当恰好一个进程拿到决定资格, 实际回执={receipts}"
        _, winner_run_id, command_id = winners[0].split()
        assert winner_run_id == run_id

        assert (await load_approval(approval_id)).status == ApprovalStatus.APPROVED.value
        # 决定与恢复命令同事务: 运行退回排队等消费者领取
        assert (await load_run(run_id)).status == RunStatus.PENDING.value
        async with SessionLocal() as db:
            commands = await run_commands_crud.list_commands(db, run_id)
        assert len(commands) == 1, "落败进程复制出了并行命令"
        assert commands[0].id == command_id
        assert commands[0].kind == RunCommandKind.RESUME.value
        assert commands[0].status == RunCommandStatus.PENDING.value
        assert commands[0].approval_id == approval_id
        # 决定资格只被消费掉一次, 审批单不会退回 pending
        async with SessionLocal() as db:
            assert await approvals_crud.has_pending_approval(db, (await load_run(run_id)).session_id) is False
    finally:
        await terminate(second)
        await terminate(first)


async def test_resume_command_left_by_dead_process_is_still_claimable():
    """T10 跨进程: 写命令的进程提交后立刻退出, 另一个进程仍能领取这条恢复命令"""
    database = await current_database()
    run_id, approval_id = await seed_pending_approval("race_t10", "race:t10")
    writer = await spawn_worker(database, "queue", approval_id, thread_id="race:t10", status="approved")
    try:
        await release(writer)
        receipt = await read_receipt(writer)
        assert receipt != "QUEUED NONE"
        _, winner_run_id, command_id = receipt.split()
        assert winner_run_id == run_id
    finally:
        await terminate(writer)
    # 写命令的进程已经消失, 等价于 API 在事务提交之后立刻崩溃
    assert writer.returncode is not None
    assert (await load_run(run_id)).status == RunStatus.PENDING.value

    # 另一个进程(等价于独立 Worker)仍能领取这条恢复命令
    assert await claim_execution(run_id, command_id) is True
    assert (await load_run(run_id)).status == RunStatus.RUNNING.value
    async with SessionLocal() as db:
        command = await run_commands_crud.get_command_by_id(db, command_id)
        assert command.status == RunCommandStatus.CLAIMED.value
        assert command.claimed_epoch == 1
        assert command.delivery_attempt == 1

    # 重复领取失败, 不会复制出并行命令
    assert await claim_execution(run_id, command_id) is False
    async with SessionLocal() as db:
        commands = await run_commands_crud.list_commands(db, run_id)
    assert len(commands) == 1
    assert (await load_run(run_id)).status == RunStatus.RUNNING.value
