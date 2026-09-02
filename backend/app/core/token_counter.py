"""模型输出token计数器"""

import json
import math
from typing import Sequence

from pydantic import BaseModel, Field
from langchain_core.messages import BaseMessage, AIMessage, convert_to_messages
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.schemas.enums import MessageRole


# 估算的 字符/token
_CHARS_PER_TOKEN = 4.

# 每条消息的框架开销
_PER_MESSAGE_OVERHEAD = 3

# 前缀合理性守卫
_PREFIX_GUARD_FACTOR = 5.0
_PREFIX_GUARD_SLACK = 1024


# 消息角色的枚举映射关系
_TYPE_TO_ROLE = {
    "system": MessageRole.SYSTEM,
    "human": MessageRole.USER,
    "ai": MessageRole.ASSISTANT,
    "tool": MessageRole.TOOL,
}


# 上下文token计数模型类
class MessageTokens(BaseModel):
    """单条message的token计数结构"""
    message_id: str | None = Field(None, description="消息id")
    role: MessageRole = Field(..., description="消息角色")
    tokens: int = Field(..., description="这条消息的token数")
    exact: bool = Field(..., description="这条消息是否是通过元数据读出的准确token数")

class ContextTokenReport(BaseModel):
    """整份上下文统计报告"""
    total: int = Field(..., description="下一次模型调用的预计输入")
    by_role: dict[str, int] = Field(default_factory=dict, description="按消息角色统计token")
    messages: list[MessageTokens] = Field(default_factory=list, description="全量消息token统计")
    overhead: int = Field(0, description="工具定义等固定token开销")
    has_usage: bool = Field(False, description="本地统计是否是以精确的元数据核算为主")


def estimate_tokens(text: str) -> int:
    """粗略估计一段文本的token数"""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))  # 粗略计算token数，向上取整

def estimate_tool_schema_tokens(tools: Sequence | None) -> int:
    """估算工具定义的固定开销"""
    if not tools:
        return 0
    schemas = [convert_to_openai_tool(t) for t in tools]  # 转化成openai工具列表形式
    return estimate_tokens(json.dumps(schemas, ensure_ascii=False))

def _content_str(msg: BaseMessage) -> str:
    """安全取出消息正文信息"""
    if isinstance(msg.content, str):
        return msg.content
    return json.dumps(msg.content, ensure_ascii=False, default=str)

def _char_weight(msg: BaseMessage) -> int:
    """一条消息的字符权重计算"""
    chars = len(_content_str(msg))
    # 只有AIMessage有tool_calls字段
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        chars += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
    # 只有ToolMessage有tool_call_id字段
    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        chars += len(tool_call_id)
    return max(1, chars)

def _approx_message_tokens(msg: BaseMessage) -> int:
    """粗略估计单条消息的总占用"""
    text = _content_str(msg)
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        text += json.dumps(tool_calls, ensure_ascii=False, default=str)
    return estimate_tokens(text) + _PER_MESSAGE_OVERHEAD

def _allocate(total: int, weights: list[int]) -> list[int]:
    """把一段已知的token总量按权重比例拆分成整数份额"""
    if not weights:
        return []
    if total <= 0:
        return [0] * len(weights)
    weight_sum = sum(weights)
    alloc = [total * w // weight_sum for w in weights]
    alloc[weights.index(max(weights))] += total - sum(alloc)
    return alloc


def count_context_tokens(messages: Sequence[BaseMessage], tools: Sequence | None = None) -> ContextTokenReport:
    """
    统计当前上下文的token占用, 给出完整报告
    
    Args:
        messages: LangGraph state 中的全量消息
        tools: 当前图绑定的工具列表

    Returns:
        返回完整的上下文报告
    """
    msgs = convert_to_messages(messages)
    n = len(msgs)
    overhead = estimate_tool_schema_tokens(tools)  # 得到工具的固定token开销

    # 找出input_tokens>0的AIMessage作为锚点
    anchors: list[tuple[int, int, int]] = []  # (消息顺序号, input_tokens, output_tokens)
    for i, msg in enumerate(msgs):
        if isinstance(msg, AIMessage):
            usage = msg.usage_metadata or {}  # 得到元数据字典
            input_tokens = usage.get("input_tokens") or 0
            if input_tokens > 0:
                anchors.append((i, input_tokens, usage.get("output_tokens") or 0))

    tokens = [0] * n
    exact = [False] * n

    # 校验
    exact_ok = bool(anchors)  # 如果没有锚点，直接降级
    # 守卫A检查是否有负增量
    if exact_ok:
        for (_, ia_in, ia_out), (_, ib_in, _) in zip(anchors, anchors[1:]):
            # 如果出现负数说明两次调用之间的历史被改写过，直接不可信
            if ib_in - ia_in - ia_out < 0:
                exact_ok = False
                break
    # 守卫B检查前缀合理性
    if exact_ok:
        first_idx, first_in, _ = anchors[0]
        prefix_sum = first_in - overhead
        prefix_estimate = sum(_approx_message_tokens(msgs[i]) for i in range(0, first_idx))
        if (prefix_sum < 0) or (prefix_sum > prefix_estimate * _PREFIX_GUARD_FACTOR + _PREFIX_GUARD_SLACK):
            exact_ok = False

    if exact_ok:
        # 前缀区域核算
        first_idx, first_in, _ = anchors[0]
        prefix_region = list(range(0, first_idx))
        prefix_sum = first_in - overhead
        if prefix_region:
            parts = _allocate(prefix_sum, [_char_weight(msgs[i]) for i in prefix_region])
            for idx, t in zip(prefix_region, parts):
                tokens[idx] = t
                exact[idx] = True
        elif prefix_sum > 0:
            # 保证总量守恒
            overhead += prefix_sum

        # 锚点自身
        for idx, _, out_tokens in anchors:
            if out_tokens > 0:
                tokens[idx] = out_tokens
                exact[idx] = True
            else:
                # usage 异常(有输入账却没输出账), 这条单独降级为估算
                tokens[idx] = _approx_message_tokens(msgs[idx])

        # 锚点之间的段
        for (ia, ia_in, ia_out), (ib, _, _) in zip(anchors, anchors[1:]):
            seg_region = list(range(ia + 1, ib))
            seg_sum = ib_in - ia_in - ia_out
            if seg_region:
                parts = _allocate(seg_sum, [_char_weight(msgs[i]) for i in seg_region])
                for idx, t in zip(seg_region, parts):
                    tokens[idx] = t
                    exact[idx] = True
            elif seg_sum > 0:
                overhead += seg_sum

        # 最后一个锚点之后
        for idx in range(anchors[-1][0] + 1, n):
            tokens[idx] = _approx_message_tokens(msgs[idx])

    # 降级，全部消息都粗估
    else:
        for idx in range(n):
            tokens[idx] = _approx_message_tokens(msgs[idx])

    # 总结报告
    by_role: dict[str, int] = {role.value: 0 for role in MessageRole}
    items: list[MessageTokens] = []
    for idx, msg in enumerate(msgs):
        # 未知消息类型视作用户类型
        role = _TYPE_TO_ROLE.get(msg.type, MessageRole.USER)
        by_role[role.value] += tokens[idx]  # 累加各个角色各自的token数
        items.append(MessageTokens(message_id=msg.id, role=role, tokens=tokens[idx], exact=exact[idx]))

    return ContextTokenReport(
        total=sum(tokens) + overhead,
        by_role=by_role,
        messages=items,
        overhead=overhead,
        has_usage=exact_ok
    )