"""图状态"""

from typing import Annotated
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """图全局状态"""
    messages: Annotated[list[BaseMessage], add_messages]


class InputState(TypedDict):
    """输入状态"""
    messages: list[BaseMessage]


class OutputState(TypedDict):
    """输出状态"""
    messages: Annotated[list[BaseMessage], add_messages]