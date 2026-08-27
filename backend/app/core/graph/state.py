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

def merge_dict_reducer(existing: dict[str, str] | None, updates: dict[str, str]) -> dict[str, str]:
    """字典合并策略函数"""
    merged = dict(existing or {})
    merged.update(updates or {})
    return merged

def concat_list_reducer(existing: list[str] | None, updates: list[str]) -> list[str]:
    """列表合并策略函数"""
    return (existing or []) + (updates or [])

class AgentState(TypedDict):
    """图全局状态"""
    messages: Annotated[list[BaseMessage], add_messages]
    todos: Annotated[list[TodoItem], todo_list_reducer]
    grants: dict  # 工具授权快照  格式要求: {"tool": [], "command": {}}
    tool_decisions: Annotated[dict[str, str], merge_dict_reducer]  # 每个工具调用的审批决定
    executed_tool_call_ids: Annotated[list[str], concat_list_reducer]  # 以及执行过的工具产生的tool_call_id
    pending_tool_call_id: NotRequired[str | None]  # 正在等待审批的工具call_id
    tool_inputs: dict[str, dict]  # 工具调用入参快照  {tool_call_id: args}


class InputState(TypedDict):
    """输入状态"""
    messages: list[BaseMessage]
    grants: NotRequired[dict]
    todos: NotRequired[list]  # 被打断任务的旧计划注入


class OutputState(TypedDict):
    """输出状态"""
    messages: Annotated[list[BaseMessage], add_messages]