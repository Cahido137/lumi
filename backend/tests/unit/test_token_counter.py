"""消息级token计数器单元测试"""

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

from app.schemas.enums import MessageRole
from app.core.token_counter import (
    estimate_tokens,
    estimate_tool_schema_tokens,
    count_context_tokens,
)


# openai格式的工具定义, 用于测试工具开销估算
_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "搜索网页",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词"}},
            "required": ["query"],
        },
    },
}


# ---------- estimate_tokens: 统一4字符/token标准 ----------

def test_estimate_tokens_empty():
    """空文本不占token"""
    assert estimate_tokens("") == 0


def test_estimate_tokens_ceil():
    """按4字符/token估算并向上取整, 宁多勿少"""
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("a" * 100) == 25


def test_estimate_tokens_non_empty_at_least_one():
    """非空文本至少算1个token"""
    assert estimate_tokens("a") == 1


def test_estimate_tokens_chinese_same_standard():
    """中文与英文使用同一套4字符/token标准, 不做特殊系数"""
    assert estimate_tokens("中文测试") == 1
    assert estimate_tokens("中文测试五") == 2


# ---------- estimate_tool_schema_tokens: 工具定义开销 ----------

def test_estimate_tool_schema_tokens_empty():
    """没有工具时开销为0"""
    assert estimate_tool_schema_tokens(None) == 0
    assert estimate_tool_schema_tokens([]) == 0


def test_estimate_tool_schema_tokens_counts_schema():
    """工具定义按序列化后的JSON文本估算开销"""
    assert estimate_tool_schema_tokens([_TOOL]) > 0


# ---------- count_context_tokens: 降级路径 ----------

def test_count_empty_messages():
    """空上下文不崩溃且总量为0"""
    report = count_context_tokens([])
    assert report.total == 0
    assert report.has_usage is False
    assert report.messages == []


def test_count_without_usage_falls_back_to_estimate():
    """没有任何usage元数据时整体降级为粗估"""
    report = count_context_tokens([SystemMessage("A" * 40), HumanMessage("B" * 20)])
    assert report.has_usage is False
    # system: 40/4 + 3条框架开销 = 13; user: 20/4 + 3 = 8
    assert report.by_role["system"] == 13
    assert report.by_role["user"] == 8
    assert report.total == 21
    assert all(m.exact is False for m in report.messages)


def test_count_zero_input_tokens_not_anchor():
    """input_tokens=0的usage(如流式中间态)不作为锚点, 整体降级"""
    msgs = [
        AIMessage("hi", usage_metadata={"input_tokens": 0, "output_tokens": 5, "total_tokens": 5}),
        HumanMessage("你好"),
    ]
    report = count_context_tokens(msgs)
    assert report.has_usage is False


# ---------- count_context_tokens: 锚点精确路径 ----------

def test_count_single_anchor_conserves_total():
    """单个锚点时总量守恒: 报告总量 = 锚点input + output"""
    msgs = [
        SystemMessage("A" * 40),
        HumanMessage("B" * 20),
        AIMessage("ok", usage_metadata={"input_tokens": 600, "output_tokens": 50, "total_tokens": 650}),
    ]
    report = count_context_tokens(msgs)
    assert report.has_usage is True
    assert report.total == 650
    # 前缀600按字符权重40:20拆分给system与user
    assert report.by_role["system"] == 400
    assert report.by_role["user"] == 200
    # 锚点自身只计输出账
    assert report.messages[2].tokens == 50
    assert report.messages[2].exact is True


def test_count_two_anchors_split_segment_by_weight():
    """两个锚点之间的增量按字符权重分摊, 余数归最大权重保证总量守恒"""
    msgs = [
        SystemMessage("A" * 40),
        HumanMessage("B" * 20),
        AIMessage("ok", usage_metadata={"input_tokens": 600, "output_tokens": 50, "total_tokens": 650}),
        HumanMessage("C" * 100),
        ToolMessage("D" * 50, tool_call_id="t1"),
        AIMessage("done", usage_metadata={"input_tokens": 1200, "output_tokens": 80, "total_tokens": 1280}),
        HumanMessage("E" * 8),
    ]
    report = count_context_tokens(msgs)
    assert report.has_usage is True
    # 段增量 = 1200 - 600 - 50 = 550, 按权重100:52拆分得362/188
    # user桶 = 前缀200 + 段362 + 尾巴估算5
    assert report.by_role["system"] == 400
    assert report.by_role["user"] == 200 + 362 + 5
    assert report.by_role["tool"] == 188
    assert report.by_role["assistant"] == 50 + 80
    # 最后一个锚点之后的尾巴只能估算: 8/4 + 3 = 5
    assert report.messages[-1].tokens == 5
    assert report.messages[-1].exact is False
    # 总量 = 前缀600 + 锚点输出130 + 段550 + 尾巴5
    assert report.total == 1285
    assert [m.exact for m in report.messages] == [True] * 6 + [False]


def test_count_anchor_without_prefix_keeps_total():
    """锚点前没有消息时, 输入账计入固定开销, 总量依然守恒"""
    msgs = [AIMessage("ok", usage_metadata={"input_tokens": 100, "output_tokens": 7, "total_tokens": 107})]
    report = count_context_tokens(msgs)
    assert report.has_usage is True
    assert report.total == 107
    assert report.overhead == 100


# ---------- count_context_tokens: 守卫降级 ----------

def test_count_negative_delta_degrades():
    """相邻锚点出现负增量说明历史被改写, 整体降级为估算"""
    msgs = [
        HumanMessage("hi"),
        AIMessage("ok", usage_metadata={"input_tokens": 600, "output_tokens": 50, "total_tokens": 650}),
        HumanMessage("x"),
        AIMessage("b", usage_metadata={"input_tokens": 400, "output_tokens": 10, "total_tokens": 410}),
    ]
    report = count_context_tokens(msgs)
    assert report.has_usage is False
    assert all(m.exact is False for m in report.messages)


def test_count_stale_anchor_degrades():
    """压缩后残留的过期大锚点与前缀规模严重不符, 守卫拦截降级"""
    msgs = [
        HumanMessage("hi"),
        AIMessage("ok", usage_metadata={"input_tokens": 50000, "output_tokens": 10, "total_tokens": 50010}),
        HumanMessage("next"),
    ]
    report = count_context_tokens(msgs)
    assert report.has_usage is False
    # 降级后全部粗估: 每条 = ceil(字符/4) + 3 = 4
    assert report.total == 12


# ---------- count_context_tokens: 报告结构 ----------

def test_count_tools_overhead_included():
    """工具定义的固定开销计入总量"""
    report = count_context_tokens([HumanMessage("B" * 20)], tools=[_TOOL])
    assert report.overhead > 0
    # 消息估算8(20/4+3) + 工具开销
    assert report.total == 8 + report.overhead


def test_count_role_mapping():
    """四种消息类型分别归入system/user/assistant/tool四个角色桶"""
    msgs = [
        SystemMessage("s"),
        HumanMessage("h"),
        AIMessage("a"),
        ToolMessage("t", tool_call_id="x"),
    ]
    report = count_context_tokens(msgs)
    assert set(report.by_role) == {"system", "user", "assistant", "tool"}
    assert all(v > 0 for v in report.by_role.values())
    assert [m.role for m in report.messages] == [
        MessageRole.SYSTEM, MessageRole.USER, MessageRole.ASSISTANT, MessageRole.TOOL
    ]


def test_count_message_id_passthrough():
    """报告保留消息id, 供后续按消息定位"""
    report = count_context_tokens([HumanMessage("hi", id="db-123")])
    assert report.messages[0].message_id == "db-123"


def test_count_accepts_tuple_messages():
    """兼容(角色, 内容)元组形式的消息输入"""
    report = count_context_tokens([("system", "你是助手"), ("user", "你好")])
    assert len(report.messages) == 2
    assert report.messages[0].role == MessageRole.SYSTEM
    assert report.messages[1].role == MessageRole.USER
