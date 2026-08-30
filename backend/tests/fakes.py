"""测试共享的假组件: 假模型/假计划器/假工具"""
import asyncio

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.core.graph import builder
from app.core.graph.builder import build_agent_graph
from app.core.graph.schemas import PlanItem, PlanOutput


class ScriptedModel:
    """按预置顺序返回响应的假模型"""

    def __init__(self, responses):
        self.responses = list(responses)

    async def ainvoke(self, messages, **kwargs):
        return self.responses.pop(0)


class SlowModel:
    """挂起指定秒数后返回的假模型, 用于取消场景"""

    def __init__(self, seconds=30, content="太慢了"):
        self.seconds = seconds
        self.content = content

    async def ainvoke(self, messages, **kwargs):
        await asyncio.sleep(self.seconds)
        return AIMessage(content=self.content)


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
