"""上下文压缩事件枚举与响应结构单元测试"""

from app.schemas.enums import EventType
from app.core.event_response import ContextWarningResponse, ContextCompactedResponse


def test_context_event_type_values():
    """事件枚举值即SSE事件名, 前端按这些字符串匹配, 不能随意改动"""
    assert EventType.CONTEXT_WARNING.value == "context_warning"
    assert EventType.CONTEXT_COMPACTED.value == "context_compacted"


def test_context_warning_response_fields():
    """警告响应携带前端画进度条需要的全部字段"""
    resp = ContextWarningResponse(
        used_tokens=80000,
        max_context_tokens=100000,
        fraction=0.8,
        message="上下文接近上限",
    )
    assert resp.used_tokens == 80000
    assert resp.max_context_tokens == 100000
    assert resp.fraction == 0.8
    assert resp.message == "上下文接近上限"


def test_context_compacted_response_fields():
    """压缩完成响应携带压缩前后对比, 供前端展示压缩效果"""
    resp = ContextCompactedResponse(
        before_tokens=90000,
        after_tokens=30000,
        summarized_message_count=12,
    )
    assert resp.after_tokens < resp.before_tokens
    assert resp.summarized_message_count == 12
