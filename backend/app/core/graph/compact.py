import logging
from dataclasses import dataclass, field
from uuid import uuid4

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.messages import BaseMessage, RemoveMessage, SystemMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from app.core.context_limits import get_compact_limits
from app.core.graph.state import AgentState, StateUpdate
from app.core.llm import get_chat_model
from app.core.token_counter import count_context_tokens

logger = logging.getLogger(__name__)


def _middleware_token_counter(messages) -> int:
    """token计数器中间件形式"""
    msg_list = [m for m in messages if isinstance(m, BaseMessage)]
    return count_context_tokens(msg_list).total


def _build_middleware(trigger) -> SummarizationMiddleware:
    """中间件构建工厂"""
    limits = get_compact_limits()
    return SummarizationMiddleware(
        model=get_chat_model(),
        trigger=trigger,
        keep=("tokens", limits.keep_tokens),  # 保留的消息token数
        token_counter=_middleware_token_counter,
        trim_tokens_to_summarize=None,
    )


def get_auto_compact_middleware() -> SummarizationMiddleware:
    """获取自动压缩上下文中间件, 上下文到达阈值自动触发"""
    limits = get_compact_limits()
    return _build_middleware(trigger=("tokens", limits.trigger_tokens))


def get_manual_compact_middleware() -> SummarizationMiddleware:
    """获取手动压缩上下文中间件, 只要存在至少一条消息就可以进行压缩"""
    return _build_middleware(trigger=("messages", 1))


@dataclass
class CompactionOutcome:
    """一次成功压缩的细节"""

    summary_message: BaseMessage  # 生成的摘要消息
    preserved_messages: list[BaseMessage]  # 原样保留的消息列表
    covered_ids: list[str] = field(default_factory=list)


def _split_system_messages(messages: list[BaseMessage]) -> tuple[list[BaseMessage], list[BaseMessage]]:
    """
    抽取出开头连续的系统提示词, 将其分离开来

    Returns:
        (开头的系统提示词, 除开开头系统提示词之外的消息列表)
    """
    protected: list[BaseMessage] = []  # 开头需要保护的系统提示词列表
    cut = 0  # 保护结束位置
    for i, msg in enumerate(messages):
        if isinstance(msg, SystemMessage):
            protected.append(msg)
            cut = i + 1
        else:
            break
    return protected, messages[cut:]


def _ensure_ids(messages: list[BaseMessage]) -> None:
    """确保每条消息都有id, 没有的现场创建id"""
    for msg in messages:
        if not msg.id:
            msg.id = str(uuid4())


async def run_compaction(
    middleware: SummarizationMiddleware, messages: list[BaseMessage]
) -> tuple[list[BaseMessage], CompactionOutcome] | None:
    """使用给定的上下文压缩中间件执行一次压缩"""
    # 剥离开系统提示词防止一起被压缩
    protected, rest = _split_system_messages(messages)
    if not rest:
        return None
    # 补齐消息id
    _ensure_ids(rest)
    input_ids = {m.id for m in rest}
    # 进行压缩操作
    update = await middleware.abefore_model({"messages": rest}, None)  # type: ignore[arg-type, typeddict-item]
    if update is None:
        return None  # 中间件判定不需要压缩
    # 过滤RemoveMessage
    new_messages = [m for m in update["messages"] if not isinstance(m, RemoveMessage)]
    # 合并
    preserved = [m for m in new_messages if m.id in input_ids]
    summaries = [m for m in new_messages if m.id not in input_ids]
    if not summaries:
        return None
    # 提取摘要覆盖消息
    preserved_ids = {m.id for m in preserved}
    covered_ids = [m.id for m in rest if m.id is not None and m.id not in preserved_ids]
    outcome = CompactionOutcome(summary_message=summaries[0], preserved_messages=preserved, covered_ids=covered_ids)
    return protected + new_messages, outcome


async def compact_node(state: AgentState) -> StateUpdate:
    """上下文压缩节点"""
    middleware = get_auto_compact_middleware()
    before_tokens = count_context_tokens(state["messages"]).total  # 得到压缩前的上下文token数
    try:
        result = await run_compaction(middleware, state["messages"])
    except Exception:
        logger.exception("上下文压缩失败")
        return {}
    if result is None:
        return {}
    final_messages, outcome = result
    after_tokens = count_context_tokens(final_messages).total
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *final_messages],  # 删除所有消息，并追加压缩后保存的消息
        "compact_covered_ids": outcome.covered_ids,
        "compact_summary_text": outcome.summary_message.text,
        "compact_before_tokens": before_tokens,
        "compact_after_tokens": after_tokens,
    }
