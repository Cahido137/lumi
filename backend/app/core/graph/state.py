"""图状态"""

from typing import Annotated, NotRequired
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from app.schemas.todos import TodoItem


def todo_list_reducer(existing: list[TodoItem] | None, updates: list[TodoItem]) -> list[TodoItem]:
    """todo 的合并策略函数"""
    merged = {t.id: t for t in (existing or [])}  # id为key，存入已有todos
    for t in updates:
        merged[t.id] = t  # 追加新的todos
    return list(merged.values())

class AgentState(TypedDict):
    """图全局状态"""
    messages: Annotated[list[BaseMessage], add_messages]
    todos: Annotated[list[TodoItem], todo_list_reducer]
    grants: dict  # 工具授权快照  格式要求: {"tool": [], "command": {}}


class InputState(TypedDict):
    """输入状态"""
    messages: list[BaseMessage]
    grants: NotRequired[dict]


class OutputState(TypedDict):
    """输出状态"""
    messages: Annotated[list[BaseMessage], add_messages]