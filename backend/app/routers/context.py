"""上下文管理相关路由"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context_limits import get_compact_limits
from app.core.deps import get_current_user, get_owned_session_or_404
from app.core.graph.compact import get_manual_compact_middleware, run_compaction
from app.core.prompts import get_system_messages
from app.core.session_runner.helpers import rebuild_history
from app.core.session_runner.state import get_session_lock, is_session_running
from app.core.token_counter import count_context_tokens
from app.crud import messages as messages_crud
from app.crud import sessions as sessions_crud
from app.db.models import User
from app.db.session import get_db
from app.schemas.context import ContextCompactResponse, ContextUsageResponse
from app.utils.response import success_response


router = APIRouter(prefix="/api/sessions", tags=["context"])


@router.get("/{session_id}/context/usage")
async def get_context_usage(
    session_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """查询指定会话的当前上下文用量"""
    await get_owned_session_or_404(db, str(session_id), current_user)
    limits = get_compact_limits()
    history = await rebuild_history(db, str(session_id))
    messages = get_system_messages() + history
    used_tokens = count_context_tokens(messages).total
    summary_text, _ = await sessions_crud.get_context_summary(db, str(session_id))
    return success_response(
        message="上下文用量查询成功",
        data=ContextUsageResponse(
            usedTokens=used_tokens,
            maxContextTokens=limits.max_context_tokens,
            fraction=round(used_tokens / limits.max_context_tokens, 4),
            warnTokens=limits.warn_tokens,
            triggerTokens=limits.trigger_tokens,
            messageCount=len(messages),
            compacted=bool(summary_text)
        )
    )

@router.post("/{session_id}/context/compact")
async def compact_context(
    session_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """手动触发上下文压缩"""
    await get_owned_session_or_404(db, str(session_id), current_user)
    # 会话正在运行时直接拒绝压缩
    if is_session_running(str(session_id)):
        raise HTTPException(status_code=409, detail="会话正在运行中, 拒绝进行上下文压缩")
    # 拿会话锁防止压缩进行到一半有新的对话开始
    async with get_session_lock(str(session_id)):
        # 重建完整消息
        messages = get_system_messages() + await rebuild_history(db, str(session_id))
        before_tokens = count_context_tokens(messages).total
        try:
            # 运行手动压缩
            result = await run_compaction(get_manual_compact_middleware(), messages)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"上下文压缩失败: {e}")
        # 如果判定为无需压缩，则返回零压缩
        if result is None:
            return success_response(
                message="上下文内容过短, 无需压缩",
                data=ContextCompactResponse(
                    beforeTokens=before_tokens,
                    afterTokens=before_tokens,
                    summarizedMessageCount=0
                )
            )
        # 成功压缩后
        final_messages, outcome = result
        after_tokens = count_context_tokens(final_messages).total
        existing_ids = await messages_crud.filter_existing_ids(db, str(session_id), outcome.covered_ids)
        if len(existing_ids) != len(outcome.covered_ids):
            raise HTTPException(status_code=500, detail="压缩结果校验失败")
        await sessions_crud.set_context_summary(db, str(session_id), outcome.summary_message.text, existing_ids[-1])
        await db.commit()
    return success_response(
        message="上下文压缩成功",
        data=ContextCompactResponse(
            beforeTokens=before_tokens,
            afterTokens=after_tokens,
            summarizedMessageCount=len(outcome.covered_ids)
        )
    )