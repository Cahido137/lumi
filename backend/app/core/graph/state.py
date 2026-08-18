"""图状态"""

from typing import Annotated
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


def todo_list_reducer(existing: list[dict] | None, updates: list[dict]) -> list[dict]:
    """todo 的合并策略函数"""
    merged = {t["id"]: t for t in (existing or [])}  # id为key，存入已有todos
    for t in updates:
        merged[t["id"]] = t  # 追加新的todos
    return list(merged.values())

class AgentState(TypedDict):
    """图全局状态"""
    messages: Annotated[list[BaseMessage], add_messages]
    todos: Annotated[list[dict], todo_list_reducer]


class InputState(TypedDict):
    """输入状态"""
    messages: list[BaseMessage]


class OutputState(TypedDict):
    """输出状态"""
    messages: Annotated[list[BaseMessage], add_messages]