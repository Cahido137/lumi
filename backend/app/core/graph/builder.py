import logging
from typing import Literal
from uuid import uuid4

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.output_parsers import PydanticOutputParser
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.core.grants import Grants
from app.core.graph.compact import compact_node
from app.core.graph.schemas import ApprovalInterrupt, PlanOutput
from app.core.graph.state import AgentState, InputState, OutputState
from app.core.llm import create_planner_llm, get_chat_model, get_planner_structured_method
from app.core.prompts import (
    PLAN_EXECUTION_PROMPT,
    PLANNER_EXISTING_PLAN_PROMPT,
    PLANNER_PROMPT,
    TOOL_FEEDBACK_EXEC_FAILED,
    TOOL_FEEDBACK_REJECTED,
    TOOL_FEEDBACK_TODO_NOT_FOUND,
)
from app.core.tools import TOOLS
from app.core.tools.todo_tool import TODO_DONE_TOOL, TODO_MARKER_TOOLS
from app.schemas.enums import ApprovalStatus, TodoStatus
from app.schemas.todos import TodoItem

# 需要审批的工具列表
APPROVAL_REQUIRED_TOOLS = ["run_shell", "write_file"]

# 构建工具名称映射字典列表
TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}

logger = logging.getLogger(__name__)


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


async def _invoke_planner(planner_llm, prompt_messages):
    """调用规划器, 失败记日志并返回None"""
    try:
        return await planner_llm.ainvoke(prompt_messages)
    except Exception:
        logger.warning("规划器调用或结构化解析失败", exc_info=True)
        return None


async def planner_node(state: AgentState) -> AgentState:
    """计划器节点"""
    task = state["messages"][-1].content  # 拿到用户的消息
    existing_todos = sorted(state.get("todos") or [], key=lambda x: x.position)

    existing_context = []
    if existing_todos:
        existing_lines = [f"{i + 1}. {t.title}" for i, t in enumerate(existing_todos)]  # 已经存在的todo列表
        existing_context = PLANNER_EXISTING_PLAN_PROMPT.format_messages(existing_plan="\n".join(existing_lines))
    # 拼接提示词
    prompt_messages = PLANNER_PROMPT.format_messages(
        existing_plan_context=existing_context,
        task=task,
    )
    method = get_planner_structured_method()
    if method == "json_mode":
        prompt_messages = prompt_messages + [
            HumanMessage(content=PydanticOutputParser(pydantic_object=PlanOutput).get_format_instructions())
        ]
    planner_llm = create_planner_llm().with_structured_output(PlanOutput, method=method)
    plans = await _invoke_planner(planner_llm, prompt_messages)

    if plans is None:
        logger.warning("规划器未返回结构化计划, 重试一次")
        plans = await _invoke_planner(planner_llm, prompt_messages)

    # 如果有旧计划没有完成并且返回了空列表则沿用旧计划
    if plans is None or not plans.todos:
        if existing_todos:
            return {}
        return {"todos": []}

    # 构建 todos 列表
    todos = [
        TodoItem(id=str(uuid4()), title=item.title, status=TodoStatus.PENDING, position=i)
        for i, item in enumerate(plans.todos)
    ]
    return {"todos": todos}


async def model_node(state: AgentState) -> AgentState:
    """大模型节点"""
    todos = sorted(state.get("todos") or [], key=lambda x: x.position)
    messages = state["messages"]
    # 仅当存在计划时才注入计划上下文
    if todos:
        plan_lines = [
            f"{todo.position + 1}. {todo.title} [{getattr(todo.status, 'value', todo.status)}] (todo_id: {todo.id})"
            for todo in todos
        ]
        plan_context = PLAN_EXECUTION_PROMPT.format_messages(plan_lines="\n".join(plan_lines))
        messages = plan_context + state["messages"]
    response = await _model_with_tools.ainvoke(messages)
    return {"messages": [response]}


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
    tc = next((t for t in msg.tool_calls if t["id"] == pending), None)
    if tc is None:
        return {"pending_tool_call_id": None}
    decision = interrupt(
        ApprovalInterrupt(tool=tc["name"], tool_input=tc["args"] or {}, tool_call_id=tc["id"]).model_dump()
    )
    decision_value = decision if isinstance(decision, str) else decision.value
    return {"tool_decisions": {pending: decision_value}, "pending_tool_call_id": None}


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
    todos_update = None  # 标记工具执行成功后同步的 todos 状态
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
        if decision is not None and decision != ApprovalStatus.APPROVED.value:
            tool_msgs.append(
                ToolMessage(name=tc_name, content=TOOL_FEEDBACK_REJECTED, tool_call_id=tc_id, status="error")
            )
            new_executed.append(tc_id)
            continue
        if tc_name in TODO_MARKER_TOOLS and tc_input:
            todo_id = tc_input.get("todo_id")
            if todo_id not in {t.id for t in (state.get("todos") or [])}:
                tool_msgs.append(
                    ToolMessage(
                        name=tc_name,
                        content=TOOL_FEEDBACK_TODO_NOT_FOUND.format(todo_id=todo_id),
                        tool_call_id=tc_id,
                        status="success",
                    )
                )
                new_executed.append(tc_id)
                continue
        # 已授权或者无需授权的工具
        try:
            result = await TOOLS_BY_NAME[tc_name].ainvoke(tc_input)
        except Exception as e:
            tool_msgs.append(
                ToolMessage(
                    name=tc_name, content=TOOL_FEEDBACK_EXEC_FAILED.format(error=e), tool_call_id=tc_id, status="error"
                )
            )
        else:
            tool_msgs.append(ToolMessage(name=tc_name, content=result, tool_call_id=tc_id, status="success"))

            # 标记工具执行后也回写图状态
            if tc_name in TODO_MARKER_TOOLS and tc_input:
                new_status = TodoStatus.DONE if tc_name == TODO_DONE_TOOL else TodoStatus.IN_PROGRESS
                todo_id = tc_input.get("todo_id")
                base = todos_update if todos_update is not None else list(state.get("todos") or [])
                todos_update = [t.model_copy(update={"status": new_status}) if t.id == todo_id else t for t in base]
        new_executed.append(tc_id)
    node_result = {
        "messages": tool_msgs,
        "executed_tool_call_ids": new_executed,
        "tool_inputs": {tc["id"]: tc["args"] or {} for tc in msg.tool_calls},  # 取出工具调用入参
    }
    if todos_update is not None:
        node_result["todos"] = todos_update
    return node_result


def router_after_model(state: AgentState) -> Literal["precheck_node", END]:
    """模型输出后路由"""
    last_msg = state["messages"][-1]
    if last_msg.tool_calls:
        return "precheck_node"
    return END


def router_after_exec(state: AgentState) -> Literal["precheck_node", "compact_node"]:
    """执行节点后路由"""
    msg = _find_tool_call_message(state)
    executed = state.get("executed_tool_call_ids") or []
    # 如果工具调用消息不为空并且所有的tool_call都执行完毕了
    if msg is not None and all(tc["id"] in executed for tc in msg.tool_calls):
        return "compact_node"
    return "precheck_node"


def build_agent_graph(checkpointer=None):
    """组装图"""
    builder = StateGraph(state_schema=AgentState, input_schema=InputState, output_schema=OutputState)
    builder.add_node("planner_node", planner_node)
    builder.add_node("compact_node", compact_node)
    builder.add_node("model_node", model_node)
    builder.add_node("precheck_node", precheck_node)
    builder.add_node("approval_node", approval_node)
    builder.add_node("exec_node", exec_node)
    builder.add_edge(START, "planner_node")
    builder.add_edge("planner_node", "compact_node")
    builder.add_edge("compact_node", "model_node")
    builder.add_conditional_edges("model_node", router_after_model, path_map=["precheck_node", END])
    builder.add_edge("precheck_node", "approval_node")
    builder.add_edge("approval_node", "exec_node")
    builder.add_conditional_edges("exec_node", router_after_exec, path_map=["precheck_node", "compact_node"])
    return builder.compile(checkpointer=checkpointer)
