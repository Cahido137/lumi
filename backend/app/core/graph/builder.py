from typing import Literal

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from app.core.graph.state import AgentState, InputState, OutputState
from app.core.llm import get_chat_model
from app.core.tools import TOOLS


# 系统提示词
SYSTEM_PROMPT = "你是一个AI智能助手, 可以使用工具完成用户的任务。回答使用中文。"


# 获得模型
_model = get_chat_model()
# 绑定工具
_model_with_tools = _model.bind_tools(tools=TOOLS)


async def model_node(state: AgentState) -> AgentState:
    """大模型节点"""
    response = await _model_with_tools.ainvoke(state["messages"])
    return {
        "messages": [response]
    }

def router(state: AgentState) -> Literal["tool_node", END]:
    """路由函数"""
    last_msg = state["messages"][-1]
    if last_msg.tool_calls:
        return "tool_node"
    return END


def build_agent_graph():
    """组装图"""
    builder = StateGraph(
        state_schema=AgentState,
        input_schema=InputState,
        output_schema=OutputState
    )
    builder.add_node("model_node", model_node)
    builder.add_node("tool_node", ToolNode(TOOLS))
    builder.add_edge(START, "model_node")
    builder.add_conditional_edges("model_node", router, path_map=["tool_node", END])
    builder.add_edge("tool_node", "model_node")
    return builder.compile()