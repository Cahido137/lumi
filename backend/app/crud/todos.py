"""todo 的 CRUD 操作"""

from sqlalchemy import update, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Todo
from app.schemas.todos import TodoItem
from app.schemas.enums import TodoStatus


async def replace_todos(db: AsyncSession, session_id: str, todos: list[TodoItem]) -> None:
    """整批替换会话的计划"""
    # 删除计划
    await db.execute(delete(Todo).where(Todo.session_id == session_id))
    # 添加新的计划
    for t in todos:
        db.add(Todo(
            id=t.id,
            session_id=session_id,
            title=t.title,
            status=t.status.value,
            position=t.position
        ))
    await db.flush()

async def update_todo_status(db: AsyncSession, todo_id: str, status: TodoStatus | str) -> None:
    """更新指定 todo 的状态"""
    if isinstance(status, TodoStatus):
        status = status.value
    await db.execute(update(Todo).where(Todo.id == todo_id).values(status=status))
    await db.flush()

async def list_todos(db: AsyncSession, session_id: str) -> list[Todo]:
    """查询指定会话的全部TODO"""
    stmt = select(Todo).where(Todo.session_id == session_id).order_by(Todo.position.asc())
    result = await db.execute(stmt)
    return list(result.scalars())

async def get_todo_by_id(db: AsyncSession, todo_id: str) -> Todo | None:
    """按ID查询todo"""
    return await db.get(Todo, todo_id)