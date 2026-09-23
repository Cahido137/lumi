"""单元测试: 提交载荷摘要的计算规则。"""

from app.core.session_runner.submission import (
    FIELD_SEPARATOR,
    SUBMIT_KIND_CHAT,
    SUBMIT_KIND_RETRY,
    build_fingerprint,
)


def test_fingerprint_is_stable_64_hex_chars():
    """同一份载荷算出同一个摘要, 长度正好是列宽 64"""
    first = build_fingerprint(SUBMIT_KIND_CHAT, session_id="s1", content="你好")
    second = build_fingerprint(SUBMIT_KIND_CHAT, session_id="s1", content="你好")
    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_fingerprint_covers_every_business_field():
    """命令种类、目标会话、目标消息与正文任一变化都产生不同摘要"""
    base = build_fingerprint(SUBMIT_KIND_CHAT, session_id="s1", content="你好")
    assert build_fingerprint(SUBMIT_KIND_RETRY, session_id="s1", content="你好") != base
    assert build_fingerprint(SUBMIT_KIND_CHAT, session_id="s2", content="你好") != base
    assert build_fingerprint(SUBMIT_KIND_CHAT, session_id="s1", content="你好呀") != base
    assert build_fingerprint(SUBMIT_KIND_CHAT, session_id="s1", content="你好", message_id="m1") != base


def test_fingerprint_treats_missing_message_id_as_empty():
    """message_id 缺省与空串等价, 聊天提交不必显式传空"""
    assert build_fingerprint(SUBMIT_KIND_RETRY, session_id="s1", content="x") == build_fingerprint(
        SUBMIT_KIND_RETRY, session_id="s1", content="x", message_id=""
    )


def test_fingerprint_field_boundary_cannot_be_shifted():
    """字段之间有分隔符, 挪动字段边界不会撞出同一个摘要"""
    shifted = build_fingerprint(SUBMIT_KIND_CHAT, session_id="s1a", content="bc")
    original = build_fingerprint(SUBMIT_KIND_CHAT, session_id="s1", content="abc")
    assert shifted != original
    assert FIELD_SEPARATOR not in original
