"""图流程测试: 假模型+假工具+内存检查点, 不依赖真实LLM与数据库"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.core.graph import builder
from app.core.graph.builder import build_agent_graph
from app.core.graph.schemas import PlanItem, PlanOutput
from app.core.grants import Grants
from app.schemas.todos import TodoItem, TodoStatus


class ScriptedModel:
    """按预置顺序返回响应的假模型"""

    def __init__(self, responses):
        self.responses = list(responses)

    async def ainvoke(self, messages, **kwargs):
        return self.responses.pop(0)


class FakePlanner:
    """返回预置计划的假计划器"""

    def __init__(self, titles):
        self._titles = titles

    def with_structured_output(self, schema, method=None):
        return self

    async def ainvoke(self, messages, **kwargs):
        return PlanOutput(todos=[PlanItem(title=t) for t in self._titles])


class FakeTool:
    """记录调用参数、按预设结果返回的假工具"""

    def __init__(self, name, result="ok", error=None, calls=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = calls if calls is not None else []

    async def ainvoke(self, args):
        self.calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result


def make_graph(monkeypatch, model, planner, tools):
    """组装被测图: 模型/计划器/工具全部换成假的, 检查点用内存实现"""
    monkeypatch.setattr(builder, "_model_with_tools", model)
    monkeypatch.setattr(builder, "create_planner_llm", lambda: planner)
    monkeypatch.setattr(builder, "TOOLS_BY_NAME", tools)
    return build_agent_graph(checkpointer=InMemorySaver())


CONFIG = {"configurable": {"thread_id": "t1"}}


async def test_planner_output_becomes_todos(monkeypatch):
    """计划器输出转为状态里的todos, 带顺序和初始状态"""
    model = ScriptedModel([AIMessage(content="你好")])
    graph = make_graph(monkeypatch, model, FakePlanner(["步骤一", "步骤二"]), {})
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="做任务")]}, CONFIG
    )
    # 图输出schema只含messages, 完整状态需从检查点读取
    state = (await graph.aget_state(CONFIG)).values
    todos = state["todos"]
    assert [t.title for t in todos] == ["步骤一", "步骤二"]
    assert [t.status for t in todos] == [TodoStatus.PENDING] * 2
    assert [t.position for t in todos] == [0, 1]
    assert result["messages"][-1].content == "你好"


async def test_non_approval_tool_executes(monkeypatch):
    """无需审批的工具直接执行, 产出success工具消息"""
    tool = FakeTool("web_search", result="找到3条结果")
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "web_search", "args": {"query": "新闻"}, "id": "call_1"}
        ]),
        AIMessage(content="搜索完成")
    ])
    graph = make_graph(monkeypatch, model, FakePlanner([]), {"web_search": tool})
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="搜新闻")]}, CONFIG
    )
    assert tool.calls == [{"query": "新闻"}]
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].status == "success"
    assert result["messages"][-1].content == "搜索完成"


async def test_approval_interrupt_then_approve(monkeypatch):
    """审批工具先中断, 批准后恢复执行"""
    tool = FakeTool("run_shell", result="目录列表")
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "run_shell", "args": {"command": "dir"}, "id": "call_1"}
        ]),
        AIMessage(content="执行完毕")
    ])
    graph = make_graph(monkeypatch, model, FakePlanner([]), {"run_shell": tool})
    first = await graph.ainvoke(
        {"messages": [HumanMessage(content="列目录")]}, CONFIG
    )
    assert "__interrupt__" in first
    assert first["__interrupt__"][0].value["tool"] == "run_shell"
    assert tool.calls == []
    result = await graph.ainvoke(Command(resume="approved"), CONFIG)
    assert tool.calls == [{"command": "dir"}]
    assert result["messages"][-1].content == "执行完毕"


async def test_approval_reject_blocks_execution(monkeypatch):
    """审批拒绝后工具不执行, 产出拒绝提示的error工具消息"""
    tool = FakeTool("run_shell")
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "run_shell", "args": {"command": "dir"}, "id": "call_1"}
        ]),
        AIMessage(content="好的, 已取消")
    ])
    graph = make_graph(monkeypatch, model, FakePlanner([]), {"run_shell": tool})
    first = await graph.ainvoke(
        {"messages": [HumanMessage(content="列目录")]}, CONFIG
    )
    assert "__interrupt__" in first
    result = await graph.ainvoke(Command(resume="rejected"), CONFIG)
    assert tool.calls == []
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs[0].content == "用户拒绝此操作"
    assert tool_msgs[0].status == "error"


async def test_granted_tool_skips_interrupt(monkeypatch):
    """已整体授权的审批工具不再中断, 直接执行"""
    tool = FakeTool("run_shell")
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "run_shell", "args": {"command": "dir"}, "id": "call_1"}
        ]),
        AIMessage(content="done")
    ])
    graph = make_graph(monkeypatch, model, FakePlanner([]), {"run_shell": tool})
    grants = Grants(tool=["run_shell"])
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="列目录")], "grants": grants.model_dump()},
        CONFIG
    )
    assert "__interrupt__" not in result
    assert tool.calls == [{"command": "dir"}]


async def test_multi_tool_message_inputs_after_resume(monkeypatch):
    """回归: 同消息混合审批/非审批工具, 恢复后两条入参快照都完整"""
    shell = FakeTool("run_shell")
    search = FakeTool("web_search")
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "run_shell", "args": {"command": "dir"}, "id": "call_1"},
            {"name": "web_search", "args": {"query": "新闻"}, "id": "call_2"},
        ]),
        AIMessage(content="完成")
    ])
    graph = make_graph(monkeypatch, model, FakePlanner([]), {
        "run_shell": shell, "web_search": search
    })
    first = await graph.ainvoke(
        {"messages": [HumanMessage(content="列目录并搜新闻")]}, CONFIG
    )
    assert "__interrupt__" in first
    result = await graph.ainvoke(Command(resume="approved"), CONFIG)
    assert shell.calls == [{"command": "dir"}]
    assert search.calls == [{"query": "新闻"}]
    state = (await graph.aget_state(CONFIG)).values
    assert state["tool_inputs"] == {
        "call_1": {"command": "dir"}, "call_2": {"query": "新闻"}
    }


async def test_todo_markers_update_status(monkeypatch):
    """todo标记工具驱动计划状态推进, start后done最终为完成"""
    markers = FakeTool("markers", calls=[])
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "mark_todo_start", "args": {"todo_id": "todo-1"}, "id": "m1"},
            {"name": "mark_todo_done", "args": {"todo_id": "todo-1"}, "id": "m2"},
        ]),
        AIMessage(content="计划完成")
    ])
    graph = make_graph(monkeypatch, model, FakePlanner([]), {
        "mark_todo_start": markers, "mark_todo_done": markers
    })
    todos = [TodoItem(id="todo-1", title="步骤1", status=TodoStatus.PENDING, position=0)]
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="继续")], "todos": todos}, CONFIG
    )
    state = (await graph.aget_state(CONFIG)).values
    assert state["todos"][0].status == TodoStatus.DONE


async def test_todo_marker_invalid_id_returns_hint(monkeypatch):
    """todo_id不存在时标记工具不执行, 返回核对提示"""
    marker = FakeTool("mark_todo_start", result="不应被调用")
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "mark_todo_start", "args": {"todo_id": "nope"}, "id": "m1"}
        ]),
        AIMessage(content="知道了")
    ])
    graph = make_graph(monkeypatch, model, FakePlanner([]), {"mark_todo_start": marker})
    todos = [TodoItem(id="todo-1", title="步骤1", status=TodoStatus.PENDING, position=0)]
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="继续")], "todos": todos}, CONFIG
    )
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs[0].content == "未找到id为nope的计划。核对ID后重试"
    assert marker.calls == []


async def test_tool_failure_marks_error(monkeypatch):
    """工具抛异常时产出error工具消息, 图继续运行不崩溃"""
    tool = FakeTool("web_search", error=RuntimeError("网络错误"))
    model = ScriptedModel([
        AIMessage(content="", tool_calls=[
            {"name": "web_search", "args": {"query": "x"}, "id": "call_1"}
        ]),
        AIMessage(content="搜索失败, 稍后重试")
    ])
    graph = make_graph(monkeypatch, model, FakePlanner([]), {"web_search": tool})
    result = await graph.ainvoke(
        {"messages": [HumanMessage(content="搜x")]}, CONFIG
    )
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tool_msgs[0].status == "error"
    assert "工具执行失败" in tool_msgs[0].content
