"""运行提交协议。

Note:
    本模块负责提交的幂等核对、准入校验、输入与运行记录落库。
"""

from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy.exc import IntegrityError

from app.core.session_runner.helpers import build_config
from app.crud import approvals as approvals_crud
from app.crud import messages as messages_crud
from app.crud import runs as runs_crud
from app.db.models import Run
from app.db.session import SessionLocal
from app.schemas.enums import MessageRole
from app.schemas.error_code import CommonErrorCode, SessionErrorCode
from app.utils.errors import ConflictError

SUBMIT_KIND_CHAT = "chat"
"""提交种类: 发起一轮新对话。"""

SUBMIT_KIND_RETRY = "retry"
"""提交种类: 用户消息重试。"""

FIELD_SEPARATOR = "\x1f"
"""载荷字段分隔符。"""


@dataclass(frozen=True, slots=True)
class Submission:
    """一次提交的受理结果。"""

    run_id: str
    """受理的运行记录ID。"""

    thread_id: str
    """本轮运行使用的 LangGraph 线程ID。"""

    input_message_id: str | None
    """本轮运行的输入消息ID。"""

    replayed: bool
    """本次提交是否为幂等重放, 为 True 时表示为此前已受理的同键请求。"""


def build_fingerprint(kind: str, *, session_id: str, content: str, message_id: str | None = None) -> str:
    """计算提交载荷的摘要 (64 字节)。

    Args:
        kind: 提交种类。
        session_id: 目标会话ID。
        content: 用户输入的正文内容。
        message_id: 重试时的目标消息ID。

    Returns:
        str: 使用 sha256 计算载荷的十六进制摘要, 固定为 64 字符。
    """
    payload = FIELD_SEPARATOR.join((kind, session_id, message_id or "", content))  # 拼接出载荷字符串
    return sha256(payload.encode("utf-8")).hexdigest()


def _as_replay(run: Run, fingerprint: str | None) -> Submission:
    """把已经存在的同键运行转换为重放受理结果。

    Args:
        run: 此前已经受理过的运行记录。
        fingerprint: 本次提交的载荷摘要字符串。

    returns:
        Submission: 标记为幂等重放的受理结果。

    Raises:
        ConflictError: 同一幂等键但是不同提交载荷。
    """
    # 出现了同一幂等键但是不同载荷的情况
    if fingerprint is not None and run.request_fingerprint != fingerprint:
        raise ConflictError(
            message="同一幂等键不同提交载荷冲突", code=CommonErrorCode.CONFLICT, detail={"request_id": run.request_id}
        )
    return Submission(run_id=run.id, thread_id=run.thread_id, input_message_id=run.input_message_id, replayed=True)


async def _resolve_integrity_conflict(
    exc: IntegrityError, session_id: str, request_id: str | None, fingerprint: str | None
) -> Submission:
    """处理唯一约束冲突异常。

    Args:
        exc: 捕获的完整性冲突。
        session_id: 目标会话ID。
        request_id: 本次提交的幂等键。
        fingerprint: 本次提交的载荷摘要。

    Returns:
        Submission: 如果为幂等重放则返回幂等重放的受理结果。
    """
    # 有幂等键的情况下检查是否有已经落库的运行记录
    if request_id is not None:
        async with SessionLocal() as db:
            existing = await runs_crud.get_run_by_request_id(db, request_id)
        if existing is not None:
            return _as_replay(existing, fingerprint)
    # 处理非幂等重放的异常
    async with SessionLocal() as db:
        active = await runs_crud.get_active_run(db, session_id)
    if active is not None:
        raise ConflictError(message="该会话存在正在运行的对话", code=SessionErrorCode.RUN_IN_PROGRESS)
    raise exc


async def submit_run(
    session_id: str,
    content: str,
    *,
    request_id: str | None = None,
    kind: str = SUBMIT_KIND_CHAT,
    user_message_id: str | None = None,
    attempt: int = 1,
) -> Submission:
    """受理一次运行提交。

    Args:
        session_id: 目标会话ID。
        content: 用户消息正文内容。
        request_id: 幂等键, 可空。为空时表示此次提交不参与幂等判断。
        kind: 提交种类。
        user_message_id: 重试时的复用已有用户消息的ID, 不是重试场景为 None。
        attempt: 同一条消息的第几次尝试。

    Returns:
        Submission: 受理结果。

    Raises:
        ConflictError: 会话存在待决审批、同键提交了不同内容, 或会话已有活动运行。
    """
    # 创建摘要指纹
    fingerprint = (
        build_fingerprint(kind, session_id=session_id, content=content, message_id=user_message_id)
        if request_id is not None
        else None
    )

    # 检查数据库中是否有已经受理落库的运行记录
    # 如果有则直接返回已存在的运行记录
    if request_id is not None:
        async with SessionLocal() as db:
            existing = await runs_crud.get_run_by_request_id(db, request_id)
        if existing is not None:
            return _as_replay(existing, fingerprint)

    # 如果没有
    async with SessionLocal() as db:
        if await approvals_crud.has_pending_approval(db, session_id):
            raise ConflictError(
                message="该会话存在未完成的审批",
                code=SessionErrorCode.PENDING_APPROVAL_EXISTS,
            )
        # 会话已有活动运行中，不受理新的提交
        active = await runs_crud.get_active_run(db, session_id)
        if active is not None:
            raise ConflictError(message="该会话存在正在运行的对话", code=SessionErrorCode.RUN_IN_PROGRESS)
        input_message_id = user_message_id
        # 代表是新的消息输入, 落库并创建config
        if input_message_id is None:
            message = await messages_crud.add_message(db, session_id, MessageRole.USER, content)
            input_message_id = message.id
        thread_id = build_config(session_id).thread_id
        try:
            # 创建新的运行记录
            run = await runs_crud.create_run(
                db,
                session_id,
                thread_id,
                input_message_id=input_message_id,
                attempt=attempt,
                request_id=request_id,
                request_fingerprint=fingerprint,
            )
            run_id = run.id
            # 将运行记录标记为开始运行
            await runs_crud.mark_run_started(db, run_id)
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            return await _resolve_integrity_conflict(exc, session_id, request_id, fingerprint)
    # 返回新运行的受理记录
    return Submission(run_id=run.id, thread_id=thread_id, input_message_id=input_message_id, replayed=False)
