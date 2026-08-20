"""计划步骤标记工具"""

from langchain_core.tools import tool

from app.crud import todos as todos_crud
from app.db.session import SessionLocal


TODO_MARKER_TOOL = "mark_todo_done"

@tool(parse_docstring=True)
async def mark_todo_done(todo_id: str) -> str:
    """
    将一个计划todo步骤标记为已完成。在完成某个todo步骤后必须调用此工具。
    
    Args:
        todo_id: 计划todo的id, 取自计划列表中每个todo自带的id

    Returns:
        标记是否成功
    """
    async with SessionLocal() as db:
        todo = await todos_crud.get_todo_by_id(db, todo_id)  # 根据id查询todo
        if todo is None:
            return f"未找到id为{todo_id}的计划。核对id后重试"
        await todos_crud.update_todo_status(db, todo_id, "done")  # 查询到了则标记为已完成
        await db.commit()
        return f"已标记完成的步骤: {todo.title}"