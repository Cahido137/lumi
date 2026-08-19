from typing import Literal
from uuid import uuid4

from langgraph.graph import StateGraph, START, END
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
from langgraph.types import interrupt

from app.core.graph.state import AgentState, InputState, OutputState
from app.core.llm import get_chat_model, create_planner_llm
from app.core.tools import TOOLS
from app.core.graph.schemas import PlanOutput
from app.utils.dict import normalize_dict


# 系统提示词
SYSTEM_PROMPT = "你是一个AI智能助手, 可以使用工具完成用户的任务。回答使用中文。"

# 计划器提示词
PLANNER_PROMPT = (
    "你是一个任务规划器, 将用户的需求拆分成多个条例清晰、有先后顺序的todo计划。"
    "要求步骤之间不能有重叠, 设计的步骤数不宜过多, 也不可为了追求步骤少而放弃了清晰的条理。"
)

# 需要审批的工具列表
APPROVAL_REQUIRED_TOOLS = ["run_shell", "write_file"]

# 构建工具名称映射字典列表
TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


# 获得模型
_model = get_chat_model()
# 绑定工具
_model_with_tools = _model.bind_tools(tools=TOOLS)


def _is_granted(grants: dict, tool_name: str, args: dict) -> bool:
    """判断工具函数是否已经被授权"""
    # 如果工具在授权中存在
    if tool_name in grants.get("tool", []):
        return True
    commands = grants.get("command", {}).get(tool_name, [])
    # 判断是否已授权指定命令
    return normalize_dict(args) in commands


async def planner_node(state: AgentState) -> AgentState:
    """计划器节点"""
    task = state["messages"][-1].content  # 拿到用户的消息
    # 让模型以结构化方式输出todos
    planner_llm = create_planner_llm().with_structured_output(
        PlanOutput,
        method="function_calling"
    )
    plans = await planner_llm.ainvoke([SystemMessage(content=PLANNER_PROMPT), HumanMessage(content=task)])

    # 构建 todos 列表
    todos = [
        {"id": str(uuid4()), "title": item.title, "status": "pending", "position": i}
        for i, item in enumerate(plans.todos)
    ]
    return {
        "todos": todos
    }

async def model_node(state: AgentState) -> AgentState:
    """大模型节点"""
    # 将计划列表序列化为文字
    plan_lines = [
        f"{t["position"] + 1}. {t["title"]} [{t["status"]}]"
        for t in sorted(state.get("todos", []), key=lambda x: x["position"])  # 按顺序排列好任务
    ]
    plan_context = SystemMessage(
        content="当前正在执行计划: \n" + "\n".join(plan_lines) + "\n严格按计划完成任务。"
    )

    response = await _model_with_tools.ainvoke([plan_context] + state["messages"])
    return {
        "messages": [response]
    }

async def tool_node(state: AgentState) -> AgentState:
    """工具执行节点"""
    last_msg = state["messages"][-1]  # 获取最后一条消息
    tool_msgs = []
    grants = state.get("grants") or {}
    # 遍历工具调用请求
    for tc in last_msg.tool_calls:
        # 是需要审批的工具并且没有已经记录的审批授权
        if tc["name"] in APPROVAL_REQUIRED_TOOLS and not _is_granted(grants, tc["name"], tc["args"] or {}):
            # 工具需要审批，此处中断
            decision = interrupt({
                "tool": tc["name"],
                "tool_input": tc["args"]
            })
            # 拒绝执行情况
            if decision == "rejected":
                tool_msgs.append(ToolMessage(
                    name=tc["name"],
                    content="[REJECTED] 用户拒绝此操作",
                    tool_call_id=tc["id"]
                ))
                continue
        # 批准或直接通过，手动调用工具
        result = await TOOLS_BY_NAME[tc["name"]].ainvoke(tc["args"])
        tool_msgs.append(ToolMessage(
            name=tc["name"],
            content=result,
            tool_call_id=tc["id"]
        ))
    return {"messages": tool_msgs}

def router(state: AgentState) -> Literal["tool_node", END]:
    """路由函数"""
    last_msg = state["messages"][-1]
    if last_msg.tool_calls:
        return "tool_node"
    return END


def build_agent_graph(checkpointer=None):
    """组装图"""
    builder = StateGraph(
        state_schema=AgentState,
        input_schema=InputState,
        output_schema=OutputState
    )
    builder.add_node("planner_node", planner_node)
    builder.add_node("model_node", model_node)
    builder.add_node("tool_node", tool_node)
    builder.add_edge(START, "planner_node")
    builder.add_edge("planner_node", "model_node")
    builder.add_conditional_edges("model_node", router, path_map=["tool_node", END])
    builder.add_edge("tool_node", "model_node")
    return builder.compile(checkpointer=checkpointer)