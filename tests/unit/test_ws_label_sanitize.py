"""ws.py _sanitize_label 单测 — JSON label 防御性过滤。"""
import json

from aistock_agent.api.ws import _sanitize_label


def test_plain_label_passes_through():
    assert _sanitize_label("正在理解你的问题") == "正在理解你的问题"


def test_empty_label_returns_default():
    assert _sanitize_label("") == "处理中..."
    assert _sanitize_label(None) == "处理中..."  # type: ignore[arg-type]


def test_json_label_replaced_with_default():
    payload = json.dumps({"goal": {"question": "查 600519"}, "plan": "direct"})
    assert _sanitize_label(payload) == "处理中..."


def test_json_array_label_replaced():
    assert _sanitize_label('["a", "b"]') == "处理中..."


def test_partial_brace_not_filtered():
    """从 { 开头但非法 JSON 不替换。"""
    assert _sanitize_label("{未闭合") == "{未闭合"
