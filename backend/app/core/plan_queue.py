"""计划步骤的内存视图。

Note:
    此类并非传统意义上的队列, 只是对计划步骤列表的包装, 没有队列的先进先出语义。
"""

from __future__ import annotations

from app.schemas.enums import TodoStatus
from app.schemas.todos import TodoItem


class PlanQueue:
    """计划步骤的包装, 提供排序与状态筛选功能。"""

    def __init__(self, items: list[TodoItem] | None = None):
        """初始化计划队列。

        Args:
            items: 初始化计划步骤列表, 为空时将创建空队列。
        """
        self.items = items or []

    @classmethod
    def from_rows(cls, rows) -> PlanQueue:
        """将数据库中的数据重建成计划队列。

        Args:
            rows: 数据库 Todo 行序列。

        Returns:
            PlanQueue: 重建后的计划队列。
        """
        items = [TodoItem(id=r.id, title=r.title, status=TodoStatus(r.status), position=r.position) for r in rows]
        return cls(items)

    def to_list(self) -> list[TodoItem]:
        """返回按 position 升序排序后的计划步骤列表。

        Returns:
            list[TodoItem]: 排序后的计划步骤列表。
        """
        return sorted(self.items, key=lambda t: t.position)

    def get_in_progress_list(self) -> list[TodoItem]:
        """获取当前状态为 in_progress 的步骤列表。

        Returns:
            list[TodoItem]: 状态为 in_progress 的步骤列表。
        """
        return [t for t in self.items if t.status == TodoStatus.IN_PROGRESS]
