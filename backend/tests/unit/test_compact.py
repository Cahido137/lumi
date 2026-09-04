"""图内上下文压缩节点单元测试"""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import app.core.graph.compact as compact
from app.core.graph.compact import (
    _ensure_ids,
    _middleware_token_counter,
    _split_system_messages,
    compact_node,
    get_auto_compact_middleware,
    get_manual_compact_middleware,
    run_compaction,
)


# ---------- 测试替身 ----------

class _FakeMiddleware:
    """压缩中间件替身: 按公开契约接收(state, runtime), 返回预置更新"""

    def __init__(self, update):
        self._update = update  # 预置的返回值, 传入Exception实例时抛出
        self.called = False
        self.received = None  # 记录收到的消息列表, 供断言使用

    async def abefore_model(self, state, runtime):
        self.called = True
        self.received = state["messages"]
        if isinstance(self._update, Exception):
            raise self._update
        return self._update


def _fake_model():
    """模型替身: 中间件构造时会调用模型的with_retry(), 按公开契约补齐"""
    model = SimpleNamespace()
    model.with_retry = lambda: model
    return model


def _patch_limits(monkeypatch, trigger=75, keep=30):
    """替换压缩阈值解析, 避免读到真实配置"""
    monkeypatch.setattr(compact, "get_compact_limits", lambda: SimpleNamespace(
        max_context_tokens=100,
        trigger_tokens=trigger,
        warn_tokens=60,
        keep_tokens=keep,
    ))


def _fake_compressed_update(summary_text, kept_messages):
    """构造中间件触发压缩时的标准返回: 删除全部指令 + 摘要消息 + 保留消息"""
    return {"messages": [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        HumanMessage(content=summary_text, id="summary-1"),
        *kept_messages,
    ]}


def _conversation():
    """模拟真实对话: 提示词 + 多轮对话(含打断消息) + 最新用户消息"""
    prompt = SystemMessage(content="你是一个AI智能助手")
    m1 = HumanMessage(content="帮我写个脚本")
    m2 = AIMessage(content="好的, 正在执行")
    cancel = SystemMessage(content="[对话已被用户打断]")
    m3 = HumanMessage(content="换个思路")
    return prompt, m1, m2, cancel, m3


# ---------- _split_system_messages: 只保护开头连续的系统消息段 ----------

def test_split_protects_leading_system_block():
    """开头的系统提示词进保护段, 中段的打断消息留在参与压缩的一侧"""
    prompt, m1, m2, cancel, m3 = _conversation()
    protected, rest = _split_system_messages([prompt, m1, m2, cancel, m3])
    assert protected == [prompt]
    assert rest == [m1, m2, cancel, m3]
    # 打断消息不能被误判为提示词
    assert cancel in rest


def test_split_no_leading_system():
    """开头不是系统消息时保护段为空, 列表原样参与压缩"""
    m1 = HumanMessage(content="你好")
    m2 = SystemMessage(content="[对话已被用户打断]")
    protected, rest = _split_system_messages([m1, m2])
    assert protected == []
    assert rest == [m1, m2]


def test_split_multiple_leading_systems():
    """开头连续多条系统消息全部进保护段, 遇到非系统消息立即停止"""
    s1 = SystemMessage(content="提示词1")
    s2 = SystemMessage(content="提示词2")
    h1 = HumanMessage(content="你好")
    s3 = SystemMessage(content="[对话已被用户打断]")
    protected, rest = _split_system_messages([s1, s2, h1, s3])
    assert protected == [s1, s2]
    assert rest == [h1, s3]


def test_split_all_system_messages():
    """列表全是系统消息时全部进保护段, 参与压缩一侧为空"""
    s1 = SystemMessage(content="提示词")
    protected, rest = _split_system_messages([s1])
    assert protected == [s1]
    assert rest == []


def test_split_empty_list():
    """空列表两侧都为空"""
    protected, rest = _split_system_messages([])
    assert protected == []
    assert rest == []


# ---------- _ensure_ids: 缺id补齐, 已有id保留 ----------

def test_ensure_ids_assigns_missing_ids():
    """没有id的消息现场补齐唯一id"""
    m1 = HumanMessage(content="a")
    m2 = AIMessage(content="b")
    _ensure_ids([m1, m2])
    assert m1.id and m2.id
    assert m1.id != m2.id


def test_ensure_ids_keeps_existing_ids():
    """已有id的消息(如数据库主键重建的消息)保持原id不动"""
    m1 = HumanMessage(content="a", id="db-primary-key-1")
    _ensure_ids([m1])
    assert m1.id == "db-primary-key-1"


# ---------- _middleware_token_counter: 转发给自己的计数器 ----------

def test_middleware_token_counter_delegates(monkeypatch):
    """过滤非消息对象后转发给消息级计数器并返回total"""
    received = []

    def _fake_count(msg_list):
        received.append(msg_list)
        return SimpleNamespace(total=42)

    monkeypatch.setattr(compact, "count_context_tokens", _fake_count)
    result = _middleware_token_counter([HumanMessage(content="a"), "杂质", {"k": 1}])
    assert result == 42
    assert len(received[0]) == 1  # 非BaseMessage被过滤


# ---------- 中间件工厂: 参数装配正确 ----------

def test_auto_middleware_uses_token_trigger(monkeypatch):
    """自动压缩中间件的触发条件是触发阈值token数"""
    _patch_limits(monkeypatch, trigger=75, keep=30)
    fake_model = _fake_model()
    monkeypatch.setattr(compact, "get_chat_model", lambda: fake_model)
    mw = get_auto_compact_middleware()
    assert mw.trigger == ("tokens", 75)
    assert mw.keep == ("tokens", 30)
    assert mw.trim_tokens_to_summarize is None  # 关闭摘要前截断
    assert mw.token_counter is _middleware_token_counter  # 用自己的精确计数器
    assert mw.model is fake_model


def test_manual_middleware_always_triggers(monkeypatch):
    """手动压缩中间件只要有一条消息就触发, 键必须是复数messages"""
    _patch_limits(monkeypatch)
    monkeypatch.setattr(compact, "get_chat_model", _fake_model)
    mw = get_manual_compact_middleware()
    assert mw.trigger == ("messages", 1)


# ---------- run_compaction: 黑盒调用与前后差集识别 ----------

@pytest.mark.asyncio
async def test_run_compaction_not_triggered():
    """中间件判定不需要压缩(返回None)时返回None"""
    _, m1, m2, cancel, m3 = _conversation()
    fake = _FakeMiddleware(update=None)
    result = await run_compaction(fake, [m1, m2, cancel, m3])
    assert result is None
    assert fake.called


@pytest.mark.asyncio
async def test_run_compaction_all_system_returns_none_without_call():
    """只有系统提示词时无可压缩内容, 直接返回None且不调用中间件"""
    prompt = SystemMessage(content="提示词")
    fake = _FakeMiddleware(update=None)
    result = await run_compaction(fake, [prompt])
    assert result is None
    assert not fake.called


@pytest.mark.asyncio
async def test_run_compaction_success():
    """压缩成功: 提示词放回最前, 差集识别保留消息/摘要消息/被覆盖id"""
    prompt, m1, m2, cancel, m3 = _conversation()
    fake = _FakeMiddleware(update=_fake_compressed_update("这是摘要", [m3]))

    final_messages, outcome = await run_compaction(fake, [prompt, m1, m2, cancel, m3])

    # 系统提示词不在中间件输入中
    assert prompt not in fake.received
    # 中间件收到的消息调用前都已补齐id
    assert all(m.id for m in fake.received)
    # 压缩后完整列表 = 保护段 + 摘要 + 保留消息
    assert final_messages[0] is prompt
    assert final_messages[1].content == "这是摘要"
    assert final_messages[2] is m3
    # 细节: 摘要消息 / 保留消息 / 被覆盖id(按原对话顺序)
    assert outcome.summary_message.content == "这是摘要"
    assert outcome.preserved_messages == [m3]
    assert outcome.covered_ids == [m1.id, m2.id, cancel.id]


@pytest.mark.asyncio
async def test_run_compaction_keeps_existing_db_ids():
    """带数据库主键id的消息, 被覆盖id直接就是数据库id"""
    m1 = HumanMessage(content="旧消息", id="db-id-1")
    m2 = HumanMessage(content="新消息", id="db-id-2")
    fake = _FakeMiddleware(update=_fake_compressed_update("摘要", [m2]))
    _, outcome = await run_compaction(fake, [m1, m2])
    assert outcome.covered_ids == ["db-id-1"]


@pytest.mark.asyncio
async def test_run_compaction_no_summary_generated():
    """防御分支: 中间件返回中没有新消息时视为未压缩"""
    m1 = HumanMessage(content="a", id="x1")
    # 返回里只有原消息, 没有摘要
    fake = _FakeMiddleware(update={"messages": [
        RemoveMessage(id=REMOVE_ALL_MESSAGES), m1
    ]})
    result = await run_compaction(fake, [m1])
    assert result is None


# ---------- compact_node: 图节点返回值形态 ----------

@pytest.mark.asyncio
async def test_compact_node_success(monkeypatch):
    """压缩成功时: 先删全部再追加压缩结果, 并落摘要文本与被覆盖id"""
    prompt, m1, m2, cancel, m3 = _conversation()
    fake = _FakeMiddleware(update=_fake_compressed_update("这是摘要", [m3]))
    monkeypatch.setattr(compact, "get_auto_compact_middleware", lambda: fake)

    update = await compact_node({"messages": [prompt, m1, m2, cancel, m3]})

    msgs = update["messages"]
    assert isinstance(msgs[0], RemoveMessage)
    assert msgs[0].id == REMOVE_ALL_MESSAGES  # 清空旧消息的指令
    assert len(msgs) == 4  # 删除指令 + 提示词 + 摘要 + 保留消息
    assert msgs[1] is prompt
    assert msgs[2].content == "这是摘要"
    assert msgs[3] is m3
    assert update["compact_covered_ids"] == [m1.id, m2.id, cancel.id]
    assert update["compact_summary_text"] == "这是摘要"


@pytest.mark.asyncio
async def test_compact_node_no_compaction(monkeypatch):
    """未触发压缩时返回空dict, 上下文原样进入模型节点"""
    fake = _FakeMiddleware(update=None)
    monkeypatch.setattr(compact, "get_auto_compact_middleware", lambda: fake)
    update = await compact_node({"messages": [HumanMessage(content="你好")]})
    assert update == {}


@pytest.mark.asyncio
async def test_compact_node_swallows_errors(monkeypatch):
    """压缩失败不中断本轮对话, 异常被吞掉并返回空dict"""
    fake = _FakeMiddleware(update=RuntimeError("摘要模型调用失败"))
    monkeypatch.setattr(compact, "get_auto_compact_middleware", lambda: fake)
    update = await compact_node({"messages": [HumanMessage(content="你好")]})
    assert update == {}


# ---------- 图接线: 每次模型调用前必经压缩节点 ----------

def test_graph_wiring_compact_before_model():
    """planner->compact->model, 工具循环结束后也经compact回流"""
    from app.core.graph.builder import build_agent_graph

    graph = build_agent_graph().get_graph()
    assert "compact_node" in graph.nodes
    direct_edges = {(e.source, e.target) for e in graph.edges if not e.conditional}
    conditional_edges = {(e.source, e.target) for e in graph.edges if e.conditional}
    # 首次模型调用前: 计划器 -> 压缩 -> 模型
    assert ("planner_node", "compact_node") in direct_edges
    assert ("compact_node", "model_node") in direct_edges
    assert ("planner_node", "model_node") not in direct_edges
    # 工具循环回流: 执行完工具先过压缩检查
    assert ("exec_node", "compact_node") in conditional_edges
    assert ("exec_node", "model_node") not in conditional_edges
