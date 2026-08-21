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

    def get_in_progress(self) -> TodoItem | None:
        """获取当前正在执行的步骤"""
        # 获取列表中正在执行的步骤
        in_progress = next(
            (t for t in self.items if t.status == TodoStatus.IN_PROGRESS), None
        )
        return in_progress

    def has_in_progress(self) -> bool:
        """检查当前计划队列中是否有正在执行的计划"""
        return self.get_in_progress() is not None

    def get_first_pending(self) -> TodoItem | None:
        """按顺序取第一个待执行步骤"""
        pending = next(
            (t for t in self.to_list() if t.status == TodoStatus.PENDING), None
        )
        return pending

    def start_next(self) -> TodoItem | None:
        """开启下一步, 将下一步从 pending 转为 in_progress, 并返回该步骤对象"""
        current = self.get_first_pending()
        if current is not None:
            current.status = TodoStatus.IN_PROGRESS
        return current

    def resolve_current(self, status: TodoStatus) -> None:
        """将当前正在执行的步骤进行状态迁移"""
        if status == TodoStatus.IN_PROGRESS:
            return
        current = self.get_in_progress()
        if current is not None:
            current.status = status