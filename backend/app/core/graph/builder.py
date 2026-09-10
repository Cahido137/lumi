"""Agent 图的节点实现与图的组装编译。

Note:
    导入本模块会自动创建主模型实例(_model)并绑定工具(_model_with_tools)。
"""

import logging
from typing import Literal
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.output_parsers import PydanticOutputParser
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.core.grants import Grants
from app.core.graph.compact import compact_node
from app.core.graph.schemas import ApprovalInterrupt, PlanOutput
from app.core.graph.state import AgentState, InputState, OutputState, StateUpdate
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

APPROVAL_REQUIRED_TOOLS = ["run_shell", "write_file"]
"""需要审批的工具名列表。"""

TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
"""工具名到工具实例的映射字典。"""

logger = logging.getLogger(__name__)


_model = get_chat_model()
"""主模型 (未绑定工具)。"""

_model_with_tools = _model.bind_tools(tools=TOOLS)
"""主模型 (已绑定工具)。"""


def _find_tool_call_message(state: AgentState):
    """倒序找到最近一条携带 tool_call 的 AIMessage。

    Args:
        state: 图状态。

    Returns:
        查找到的 AIMessage, 未查找到返回 None。
    """
    # 倒序开始寻找
    for msg in reversed(state.get("messages") or []):
        if getattr(msg, "tool_calls", None):
            return msg
    return None


async def _invoke_planner(planner_llm, prompt_messages):
    """调用规划器, 失败记日志并返回 None。

    Args:
        planner_llm: 已绑定结构化输出的规划器模型。
        prompt_messages: 提示词消息列表。

    Returns:
        调用成功后的结构化输出, 调用或结构化解析失败时返回 None。
    """
    try:
        return await planner_llm.ainvoke(prompt_messages)
    except Exception:
        logger.warning("规划器调用或结构化解析失败", exc_info=True)
        return None


async def planner_node(state: AgentState) -> StateUpdate:
    """计划器节点: 把本轮消息拆解成计划列表。

    Args:
        state: 图全局状态。

    Returns:
        StateUpdate: 含有新计划列表的部分更新, 没有变更时返回空字典。

    Note:
        当已存在计划时会将旧计划一并交给规划器, 由其决定沿用旧计划(返回空列表)或新建。
        新建的步骤使用新 id, 图状态内按 id upsert, 旧步骤不会被移除;
        数据库侧的计划列表由会话运行器整表替换, 与图内合并语义无关。
        规划器调用或结构化解析失败后重试一次; 仍失败时沿用旧计划, 无旧计划则返回空列表。
    """
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


async def model_node(state: AgentState) -> StateUpdate:
    """大模型节点: 调用绑定工具的大模型, 负责产出工具调用和回复, 核心节点。

    Args:
        state: 全局图状态。

    Returns:
        StateUpdate: 仅返回 messages 字段, 包含模型生成的 AIMessage。

    Note:
        仅当计划存在时才注入计划上下文至系统消息。
    """
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


async def precheck_node(state: AgentState) -> StateUpdate:
    """预审批节点, 负责标记下一条需要人工审批的工具调用。

    Args:
        state: 全局图状态。

    Returns:
        StateUpdate: 含有 pending_tool_call_id, 如果没有待审批的调用则返回 None。

    Note:
        此节点每次只会逐个审批工具调用。
        遇到无需审批的工具、已经做过审批的工具、先前已经授权过的直接跳过。
    """
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


async def approval_node(state: AgentState) -> StateUpdate:
    """审批节点, 负责为指定 pending_tool_call_id 的工具发起或者恢复审批中断。

    Args:
        state: 全局图状态。

    Returns:
        StateUpdate: 以 pending_tool_call_id 为键把审批决定写入 tool_decisions, 并清空 pending_tool_call_id。

    Note:
        此节点调用 interrupt() 进行中断, 中断恢复后从此节点往下执行。
        如果没有 pending_tool_call_id, 此节点不会产生中断, 会直接透传。
    """
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


async def exec_node(state: AgentState) -> StateUpdate:
    """工具执行节点, 负责执行本轮模型消息中已经受批准的工具调用。

    Args:
        state: 全局图状态。

    Returns:
        StateUpdate: 工具结果消息、已执行 id 账目、入参快照, 以及可能的 todos 更新。

    Note:
        本节点会根据 tool_call_id, 跳过已经执行过的工具调用, 防止工具的重复调用。
        工具执行异常会回填 status 字段为 error, 告知模型。
        被拒绝的调用不会执行, 同样回填 status 字段为 error, 告知模型。
        需要审批但是还没有进行审批的会在本节点跳过, 等到审批后再进入本节点。
    """
    msg = _find_tool_call_message(state)
    if msg is None:
        return {}
    grants = Grants.model_validate(state.get("grants") or {})
    decisions = dict(state.get("tool_decisions") or {})
    executed = list(state.get("executed_tool_call_ids") or [])
    tool_msgs: list[BaseMessage] = []
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
    node_result: StateUpdate = {
        "messages": tool_msgs,
        "executed_tool_call_ids": new_executed,
        "tool_inputs": {tc["id"]: tc["args"] or {} for tc in msg.tool_calls},  # 取出工具调用入参
    }
    if todos_update is not None:
        node_result["todos"] = todos_update
    return node_result


def router_after_model(state: AgentState) -> Literal["precheck_node", "__end__"]:
    """模型输出后路由。

    模型消息带有工具调用则进入预审批节点;
    模型消息不带有工具调用、纯文本回复则结束本轮图运行并输出结果。
    """
    last_msg = state["messages"][-1]
    if getattr(last_msg, "tool_calls", None):
        return "precheck_node"
    return "__end__"


def router_after_exec(state: AgentState) -> Literal["precheck_node", "compact_node"]:
    """执行节点后路由。

    本轮最近一条 AIMessage 中携带的工具调用全部执行完毕后跳转至压缩节点开启下一轮思考,
    否则会到预审批节点, 直到执行完所有工具调用。
    """
    msg = _find_tool_call_message(state)
    executed = state.get("executed_tool_call_ids") or []
    # 如果工具调用消息不为空并且所有的tool_call都执行完毕了
    if msg is not None and all(tc["id"] in executed for tc in msg.tool_calls):
        return "compact_node"
    return "precheck_node"


def build_agent_graph(checkpointer=None):
    """组装并编译 Agent 图。

    Args:
        checkpointer: LangGraph 检查点保存器, 可空。

    Returns:
        编译后的可执行图。

    Note:
        当 checkpointer 为 None 时, 图不支持中断。
    """
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
