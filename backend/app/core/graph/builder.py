from typing import Literal
from uuid import uuid4

from langgraph.graph import StateGraph, START, END
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage
from langgraph.types import interrupt

from app.core.graph.state import AgentState, InputState, OutputState
from app.core.llm import get_chat_model, create_planner_llm
from app.core.tools import TOOLS
from app.core.graph.schemas import PlanOutput, ApprovalInterrupt
from app.core.grants import Grants
from app.schemas.todos import TodoItem
from app.schemas.enums import TodoStatus, ApprovalStatus


# 系统提示词
SYSTEM_PROMPT = "你是一个AI智能助手, 可以使用工具完成用户的任务。回答使用中文。"

# 计划器提示词
PLANNER_PROMPT = (
    "你是一个任务规划器, 将用户的需求拆分成多个条例清晰、有先后顺序的todo计划。"
    "要求步骤之间不能有重叠, 设计的步骤数不宜过多, 也不可为了追求步骤少而放弃了清晰的条理。"
    "如果用户的需求足够简单, 无需分步即可直接回答, 可以返回空的todos列表。"
)

# 需要审批的工具列表
APPROVAL_REQUIRED_TOOLS = ["run_shell", "write_file"]

# 构建工具名称映射字典列表
TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}

# 审批消息前缀
REJECTED_PREFIX = "[REJECTED]"
SKIPPED_PREFIX = "[SKIPPED]"


# 获得模型
_model = get_chat_model()
# 绑定工具
_model_with_tools = _model.bind_tools(tools=TOOLS)


def _find_tool_call_message(state: AgentState):
    """找到最近一条携带tool_call的AIMessage"""
    # 倒序开始寻找
    for msg in reversed(state.get("messages") or []):
        if getattr(msg, "tool_calls", None):
            return msg
    return None


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
        TodoItem(id=str(uuid4()), title=item.title, status=TodoStatus.PENDING, position=i)
        for i, item in enumerate(plans.todos)
    ]
    return {
        "todos": todos
    }

async def model_node(state: AgentState) -> AgentState:
    """大模型节点"""
    todos = sorted(state.get("todos") or [], key=lambda x: x.position)
    messages = state["messages"]
    # 仅当存在计划时才注入计划上下文
    if todos:
        plan_lines = [
            f"{todo.position + 1}. {todo.title} [{todo.status}] (todo_id: {todo.id})"
            for todo in todos
        ]
        plan_context = SystemMessage(
            content=(
                "当前正在执行计划: \n" + "\n".join(plan_lines) +
                "\n严格按计划完成任务。"
                "\n开始执行某个计划前必须调用 mark_todo_start 工具。"
                "\n完成一个计划后必须调用 mark_todo_done 工具。"
            )
        )
        messages = [plan_context] + state["messages"]
    response = await _model_with_tools.ainvoke(messages)
    return {
        "messages": [response]
    }

async def precheck_node(state: AgentState) -> AgentState:
    """预审批节点, 负责标记下一条需要人工审批的工具调用"""
    msg = _find_tool_call_message(state)  # 先看有没有带有tool_call_id的消息
    if msg is None:
        return {}
    grants: Grants = Grants.model_validate(state.get("grants") or {})
    decisions = dict(state.get("tool_decisions") or {})
    for tc in msg.tool_calls:
        tc_id = tc["id"]
        tc_name = tc["name"]
        tc_input = tc["args"]
        # 无需审批的工具直接跳过
        if tc_name not in APPROVAL_REQUIRED_TOOLS:
            continue
        # 已经做过审批的工具直接跳过
        if tc_id in decisions:
            continue
        # 已经授权的直接跳过
        if grants.is_granted(tc_name, tc_input or {}):
            continue
        return {"pending_tool_call_id": tc_id}
    return {"pending_tool_call_id": None}

async def approval_node(state: AgentState) -> AgentState:
    """审批节点, 负责为指定pending_tool_call_id的工具发起或者恢复审批中断"""
    pending = state.get("pending_tool_call_id")
    if not pending:
        return {}
    msg = _find_tool_call_message(state)
    tc = next(
        (t for t in msg.tool_calls if t["id"] == pending), None
    )
    if tc is None:
        return {"pending_tool_call_id": None}
    decision = interrupt(
        ApprovalInterrupt(tool=tc["name"], tool_input=tc["args"] or {}).model_dump()
    )
    decision_value = decision if isinstance(decision, str) else decision.value
    return {
        "tool_decisions": {pending: decision_value},
        "pending_tool_call_id": None
    }

async def exec_node(state: AgentState) -> AgentState:
    """工具执行节点"""
    msg = _find_tool_call_message(state)
    if msg is None:
        return {}
    grants = Grants.model_validate(state.get("grants") or {})
    decisions = dict(state.get("tool_decisions") or {})
    executed = list(state.get("executed_tool_call_ids") or [])
    tool_msgs = []
    new_executed = []
    for tc in msg.tool_calls:
        tc_id = tc["id"]
        tc_name = tc["name"]
        tc_input = tc["args"]
        # 如果已经执行过了直接跳过
        if tc_id in executed:
            continue
        decision = decisions.get(tc_id)  # 拿到这一个工具调用的审批决定
        # 如果需要审批但还没有审批
        if tc_name in APPROVAL_REQUIRED_TOOLS and decision is None and not grants.is_granted(tc_name, tc_input or {}):
            continue
        # 如果已经被拒绝
        if decision == ApprovalStatus.REJECTED.value:
            tool_msgs.append(ToolMessage(
                name=tc_name,
                content=f"{REJECTED_PREFIX} 用户拒绝此操作",
                tool_call_id=tc_id
            ))
            new_executed.append(tc_id)
            continue
        # 已授权或者无需授权的工具
        result = await TOOLS_BY_NAME[tc_name].ainvoke(tc_input)
        tool_msgs.append(ToolMessage(
            name=tc_name,
            content=result,
            tool_call_id=tc_id
        ))
        new_executed.append(tc_id)
    return {
        "messages": tool_msgs,
        "executed_tool_call_ids": new_executed
    }

def router_after_model(state: AgentState) -> Literal["precheck_node", END]:
    """模型输出后路由"""
    last_msg = state["messages"][-1]
    if last_msg.tool_calls:
        return "precheck_node"
    return END

def router_after_exec(state: AgentState) -> Literal["precheck_node", "model_node"]:
    """执行节点后路由"""
    msg = _find_tool_call_message(state)
    executed = state.get("executed_tool_call_ids") or []
    # 如果工具调用消息不为空并且所有的tool_call都执行完毕了
    if msg is not None and all(tc["id"] in executed for tc in msg.tool_calls):
        return "model_node"
    return "precheck_node"


def build_agent_graph(checkpointer=None):
    """组装图"""
    builder = StateGraph(
        state_schema=AgentState,
        input_schema=InputState,
        output_schema=OutputState
    )
    builder.add_node("planner_node", planner_node)
    builder.add_node("model_node", model_node)
    builder.add_node("precheck_node", precheck_node)
    builder.add_node("approval_node", approval_node)
    builder.add_node("exec_node", exec_node)
    builder.add_edge(START, "planner_node")
    builder.add_edge("planner_node", "model_node")
    builder.add_conditional_edges("model_node", router_after_model, path_map=["precheck_node", END])
    builder.add_edge("precheck_node", "approval_node")
    builder.add_edge("approval_node", "exec_node")
    builder.add_conditional_edges("exec_node", router_after_exec, path_map=["precheck_node", "model_node"])
    return builder.compile(checkpointer=checkpointer)