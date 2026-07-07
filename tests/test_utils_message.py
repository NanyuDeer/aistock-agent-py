"""utils.message 测试 — 消息提取工具（消除各 agent 重复遍历循环）"""

from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.utils.message import (
    extract_final_ai_response,
    extract_last_human_message,
)

# ── extract_final_ai_response ──────────────────────────────────────


def test_final_ai_response_finds_last_ai():
    messages = [
        AIMessage(content="第一回复"),
        HumanMessage(content="再问一次"),
        AIMessage(content="最终回复"),
    ]
    assert extract_final_ai_response(messages) == "最终回复"


def test_final_ai_response_skips_empty_content():
    messages = [AIMessage(content=""), AIMessage(content="有效回复")]
    assert extract_final_ai_response(messages) == "有效回复"


def test_final_ai_response_no_ai_returns_empty():
    messages = [HumanMessage(content="你好")]
    assert extract_final_ai_response(messages) == ""


def test_final_ai_response_empty_list():
    assert extract_final_ai_response([]) == ""


def test_final_ai_response_non_str_content_coerced():
    """多模态内容（list）被 str() 转换，不抛异常"""
    msg = AIMessage(content=[{"type": "text", "text": "hello"}])
    result = extract_final_ai_response([msg])
    assert isinstance(result, str)
    assert "hello" in result


# ── extract_last_human_message ─────────────────────────────────────


def test_last_human_message_base_message():
    messages = [HumanMessage(content="用户问题"), AIMessage(content="回复")]
    assert extract_last_human_message(messages) == "用户问题"


def test_last_human_message_dict_role():
    """兼容 dict 形态（role=user）的消息"""
    messages = [{"role": "user", "content": "dict 消息"}]
    assert extract_last_human_message(messages) == "dict 消息"


def test_last_human_message_returns_last():
    messages = [
        HumanMessage(content="第一条"),
        HumanMessage(content="第二条"),
    ]
    assert extract_last_human_message(messages) == "第二条"


def test_last_human_message_no_human_returns_empty():
    messages = [AIMessage(content="回复")]
    assert extract_last_human_message(messages) == ""


def test_last_human_message_empty_list():
    assert extract_last_human_message([]) == ""
