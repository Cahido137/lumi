"""计划队列"""
from app.schemas.enums import TodoStatus
from app.schemas.todos import TodoItem


class PlanQueue:
    def __init__(self, items: list[TodoItem] | None = None):
        self.items = items or []

    @classmethod
    def from_rows(cls, rows) -> PlanQueue:
        """将数据库中的数据重建成计划队列"""
        items = [
            TodoItem(id=r.id, title=r.title, status=TodoStatus(r.status), position=r.position)
            for r in rows
        ]
        return cls(items)

    def to_list(self) -> list[TodoItem]:
        """返回排序后的列表"""
        return sorted(self.items, key=lambda t: t.position)

    def get_in_progress_list(self) -> list[TodoItem]:
        """获取当前正在执行的步骤列表"""
        return [
            t for t in self.items if t.status == TodoStatus.IN_PROGRESS
        ]