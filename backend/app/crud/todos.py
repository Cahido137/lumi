"""计划表的 CRUD 操作。

Note:
    本模块的所有写操作均只 flush 不 commit, 事务边界由调用方决定。
"""

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Todo
from app.schemas.enums import TodoStatus
from app.schemas.todos import TodoItem


async def replace_todos(db: AsyncSession, session_id: str, todos: list[TodoItem]) -> None:
    """整批替换指定会话的计划列表。

    Args:
        session_id: 会话ID。
        todos: 计划列表。
    """
    # 删除计划
    await db.execute(delete(Todo).where(Todo.session_id == session_id))
    # 添加新的计划
    for t in todos:
        db.add(Todo(id=t.id, session_id=session_id, title=t.title, status=t.status.value, position=t.position))
    await db.flush()


async def update_todo_status(db: AsyncSession, todo_id: str, status: TodoStatus | str) -> None:
    """更新指定计划步骤的状态。

    Args:
        todo_id: 指定步骤ID。
        status: 计划步骤状态 TodoStatus 枚举或其字符串字面量。
    """
    if isinstance(status, TodoStatus):
        status = status.value
    await db.execute(update(Todo).where(Todo.id == todo_id).values(status=status))
    await db.flush()


async def list_todos(db: AsyncSession, session_id: str) -> list[Todo]:
    """查询指定会话的全部步骤。

    Args:
        session_id: 会话ID。

    Returns:
        会话中当前的步骤列表。
    """
    stmt = select(Todo).where(Todo.session_id == session_id).order_by(Todo.position.asc())
    result = await db.execute(stmt)
    return list(result.scalars())


async def get_todo_by_id(db: AsyncSession, todo_id: str) -> Todo | None:
    """按主键ID查询一个步骤。

    Args:
        todo_id: 指定步骤ID。

    Returns:
        如果存在指定步骤, 返回其 Todo 对象; 如果不存在, 返回 None。
    """
    return await db.get(Todo, todo_id)
