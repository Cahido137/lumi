"""图状态定义与状态各字段的合并策略。"""

from typing import Annotated, NotRequired

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from app.schemas.todos import TodoItem


def todo_list_reducer(existing: list[TodoItem] | None, updates: list[TodoItem]) -> list[TodoItem]:
    """计划列表的合并策略函数。

    本函数按 id 做合并, 遇到相同 id 做覆盖。

    Args:
        existing: 合并前的计划列表, 可空。
        updates: 更新携带的计划列表。

    Returns:
        list[TodoItem]: 合并后的计划列表。
    """
    merged = {t.id: t for t in (existing or [])}  # id为key，存入已有todos
    for t in updates:
        merged[t.id] = t  # 追加新的todos
    return list(merged.values())


def merge_dict_reducer(existing: dict[str, str] | None, updates: dict[str, str]) -> dict[str, str]:
    """字典合并策略函数。

    本函数按键进行合并, 遇到相同的键新键值会覆盖旧键值。

    Args:
        existing: 合并前的字典, 可空。
        updates: 更新携带的字典。

    Returns:
        dict[str, str]: 合并后的字典。
    """
    merged = dict(existing or {})
    merged.update(updates or {})
    return merged


def concat_list_reducer(existing: list[str] | None, updates: list[str]) -> list[str]:
    """列表合并策略函数。

    本函数做追加而不去重。

    Args:
        existing: 合并前的列表, 可空。
        updates: 更新携带的列表。

    Returns:
        list[str]: 拼接后的列表。
    """
    return (existing or []) + (updates or [])


class AgentState(TypedDict):
    """图全局状态。

    字段说明:
        messages: 对话消息列表, 使用 add_messages 合并策略进行追加合并。
        todos: 计划列表, 使用 todo_list_reducer 合并策略按 id 进行覆盖合并。
        grants: 工具的会话授权快照, 格式要求: {"tool": [], "command": {}}。
        tool_decisions: 每个工具调用的审批决定, 使用 merge_dict_reducer 策略进行按键合并。
        executed_tool_call_ids: 已执行过的 tool_call_id 账目, 使用 concat_list_reducer 合并策略进行拼接合并。
        pending_tool_call_id: 正在等待审批的 tool_call_id。
        tool_inputs: 工具调用入参快照, 格式要求: {tool_call_id: args}
        session_id: 所属会话ID。
        compact_covered_ids: 历次摘要中被压缩掉的消息id列表, 使用 concat_list_reducer 合并策略进行拼接合并。
        compact_summary_text: 最近一次压缩生成的上下文摘要文本。
        compact_before_tokens: 压缩前的上下文总 token 数。
        compact_after_tokens: 压缩后的上下文总 token 数。
    """

    messages: Annotated[list[BaseMessage], add_messages]
    todos: Annotated[list[TodoItem], todo_list_reducer]
    grants: dict  # 工具授权快照  格式要求: {"tool": [], "command": {}}
    tool_decisions: Annotated[dict[str, str], merge_dict_reducer]  # 每个工具调用的审批决定
    executed_tool_call_ids: Annotated[list[str], concat_list_reducer]  # 以及执行过的工具产生的tool_call_id
    pending_tool_call_id: NotRequired[str | None]  # 正在等待审批的工具call_id
    tool_inputs: dict[str, dict]  # 工具调用入参快照  {tool_call_id: args}
    session_id: NotRequired[str]
    compact_covered_ids: Annotated[list[str], concat_list_reducer]  # 历次摘要中被压缩掉的消息id列表
    compact_summary_text: NotRequired[str]  # 最近一次压缩生成的摘要文本
    compact_before_tokens: NotRequired[int]  # 压缩前的上下文总token数
    compact_after_tokens: NotRequired[int]  # 压缩后上下文总token数


class StateUpdate(TypedDict, total=False):
    """节点返回的部分更新状态。

    Note:
        total=False 表示所有键均可以省略, 节点只返回更新的部分字段。
        此结构用于 LangGraph 节点返回全局状态的部分字段更新。
    """

    messages: list[BaseMessage]
    todos: list[TodoItem]
    grants: dict  # 工具授权快照  格式要求: {"tool": [], "command": {}}
    tool_decisions: dict[str, str]  # 每个工具调用的审批决定
    executed_tool_call_ids: list[str]  # 执行过的工具产生的tool_call_id
    pending_tool_call_id: str | None  # 正在等待审批的工具call_id
    tool_inputs: dict[str, dict]  # 工具调用入参快照  {tool_call_id: args}
    session_id: str
    compact_covered_ids: list[str]  # 历次摘要中被压缩掉的消息id列表
    compact_summary_text: str  # 最近一次压缩生成的摘要文本
    compact_before_tokens: int  # 压缩前的上下文总token数
    compact_after_tokens: int  # 压缩后上下文总token数


class InputState(TypedDict):
    """图输入状态。

    Note:
        todos 字段用于人工打断场景, 将上一次运行的计划列表注入本次运行。
    """

    messages: list[BaseMessage]
    grants: NotRequired[dict]
    todos: NotRequired[list]  # 被打断任务的旧计划注入
    session_id: NotRequired[str]


class OutputState(TypedDict):
    """图输出状态。"""

    messages: Annotated[list[BaseMessage], add_messages]
