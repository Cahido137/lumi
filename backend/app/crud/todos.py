"""todo 的 CRUD 操作"""

from sqlalchemy import update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Todo


async def replace_todos(db: AsyncSession, session_id: str, todos: list[dict]) -> None:
    """整批替换会话的计划"""
    # 删除计划
    await db.execute(delete(Todo).where(Todo.session_id == session_id))
    # 添加新的计划
    for t in todos:
        db.add(Todo(
            id=t["id"],
            session_id=t["session_id"],
            title=t["title"],
            status=t["status"],
            position=t["position"]
        ))
    await db.flush()

async def update_todo_status(db: AsyncSession, todo_id: str, status: str) -> None:
    """更新指定 todo 的状态"""
    await db.execute(update(Todo).where(Todo.id == todo_id).values(status=status))
    await db.flush()