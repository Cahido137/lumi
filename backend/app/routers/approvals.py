"""审批相关路由。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_owned_approval_or_404
from app.core.session_runner import RunCancelledError, resume_agent_session
from app.db.models import User
from app.db.session import get_db
from app.schemas.approvals import ApprovalDecisionRequest
from app.schemas.enums import ApprovalStatus
from app.utils.response import success_response

router = APIRouter(prefix="/api/approvals", tags=["approval"])


@router.post("/{approval_id}/decide")
async def decide_approval(
    approval_id: UUID,
    request: ApprovalDecisionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """对指定审批单做出审批, 并恢复被中断的图执行。

    Returns:
        JSONResponse: 三段式信封。
        恢复后如果再次触发审批, data 为 None 且 message 提示等待审批;
        若本轮执行完成, data 传递 reply 字段;
        若恢复过程被打断, data 为 None 且 message 为打断说明。

    Raises:
        HTTPException 401: 未登录或令牌无效。
        HTTPException 404: 审批单不存在或不属于当前用户。
        HTTPException 400: 审批单已被处理, 或非法的状态值。
    """
    await get_owned_approval_or_404(db, str(approval_id), current_user)
    try:
        reply = await resume_agent_session(str(approval_id), ApprovalStatus(request.status), request.scope)
    except RunCancelledError as e:
        return success_response(message=e.message, data=None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # 再次遇到中断
    if reply is None:
        return success_response(message="已收到决定, 新任务等待审批", data=None)
    return success_response(message="已收到决定, 任务已完成", data={"reply": reply})
