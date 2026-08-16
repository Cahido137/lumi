"""图状态"""

from typing import Annotated
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """图全局状态"""
    messages: Annotated[list[BaseMessage], add_messages]
    user_input: str


class InputState(TypedDict):
    """输入状态"""
    user_input: str


class OutputState(TypedDict):
    """输出状态"""
    messages: Annotated[list[BaseMessage], add_messages]