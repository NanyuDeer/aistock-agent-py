"""双层输出解析器单元测试

覆盖 parse_event_output() 的所有解析策略和降级路径。
"""

import json

import pytest
from langchain_core.messages import AIMessage

from aistock_agent.utils.output_parser import parse_event_output


# ── parse_event_output 核心路径 ──


def test_parse_valid_json_double_output():
    """完整 JSON 含 display_report + podcast_brief → 两者均正确提取"""
    payload = json.dumps({
        "display_report": {"event_title": "测试事件", "impact_level": 4},
        "podcast_brief": "今日事件分析摘要",
        "schema_version": "2.0",
    })
    messages = [AIMessage(content=payload)]
    display, brief = parse_event_output(messages)

    assert isinstance(display, dict)
    assert display["event_title"] == "测试事件"
    assert display["impact_level"] == 4
    assert brief == "今日事件分析摘要"


def test_parse_json_display_only():
    """JSON 只有 display_report 无 podcast_brief → display_report 正常，brief 为 None"""
    payload = json.dumps({
        "display_report": {"event_title": "仅展示层"},
    })
    messages = [AIMessage(content=payload)]
    display, brief = parse_event_output(messages)

    assert isinstance(display, dict)
    assert display["event_title"] == "仅展示层"
    assert brief is None


def test_parse_json_brief_only():
    """JSON 只有 podcast_brief 无 display_report → display_report 为 None，brief 正常"""
    payload = json.dumps({
        "podcast_brief": "只有播报摘要",
    })
    messages = [AIMessage(content=payload)]
    display, brief = parse_event_output(messages)

    assert display is None
    assert brief == "只有播报摘要"


def test_parse_markdown_code_block():
    """LLM 输出包裹在 ```json ... ``` 中 → 正常解析"""
    payload = json.dumps({
        "display_report": {"event_title": "代码块内"},
        "podcast_brief": "代码块摘要",
    })
    text = f"下面是对事件的分析：\n```json\n{payload}\n```\n以上分析仅供参考。"
    messages = [AIMessage(content=text)]
    display, brief = parse_event_output(messages)

    assert isinstance(display, dict)
    assert display["event_title"] == "代码块内"
    assert brief == "代码块摘要"


def test_parse_bare_code_block_no_lang():
    """LLM 输出用 ``` ... ``` (无 json 标注) → 正常解析"""
    payload = json.dumps({
        "display_report": {"event_title": "无语言标注"},
        "podcast_brief": "裸代码块",
    })
    text = f"```\n{payload}\n```"
    messages = [AIMessage(content=text)]
    display, brief = parse_event_output(messages)

    assert isinstance(display, dict)
    assert display["event_title"] == "无语言标注"
    assert brief == "裸代码块"


def test_parse_nested_json_block():
    """JSON 块嵌在大段文本中 → 正则匹配到花括号包围的 JSON"""
    payload = json.dumps({
        "display_report": {"event_title": "嵌套"},
        "podcast_brief": "嵌套摘要",
    })
    text = f"前面有很多文字描述...\n最终结论如下：{payload}\n以上。"
    messages = [AIMessage(content=text)]
    display, brief = parse_event_output(messages)

    assert isinstance(display, dict)
    assert display["event_title"] == "嵌套"
    assert brief == "嵌套摘要"


# ── 降级/异常路径 ──


def test_parse_empty_messages():
    """消息列表为空 → 返回 (None, None)"""
    display, brief = parse_event_output([])
    assert display is None
    assert brief is None


def test_parse_no_ai_message():
    """消息列表不含 AIMessage → 返回 (None, None)"""
    messages: list[object] = []
    display, brief = parse_event_output(messages)
    assert display is None
    assert brief is None


def test_parse_ai_message_empty_content():
    """AIMessage content 为空字符串 → 返回 (None, None)"""
    messages = [AIMessage(content="")]
    display, brief = parse_event_output(messages)
    assert display is None
    assert brief is None


def test_parse_invalid_json_no_braces():
    """文本不含任何 JSON 对象 → 返回 (None, None)"""
    messages = [AIMessage(content="这是一段纯文本，没有 JSON 对象。")]
    display, brief = parse_event_output(messages)
    assert display is None
    assert brief is None


def test_parse_truncated_json():
    """JSON 被截断（花括号不完整）→ 返回 (None, None)"""
    messages = [AIMessage(content='{"display_report": {"event_title": "截断')]
    display, brief = parse_event_output(messages)
    assert display is None
    assert brief is None


def test_parse_display_report_not_dict():
    """display_report 是字符串而非 dict → 返回 (None, None) 因为 _extract_fields 过滤"""
    payload = json.dumps({
        "display_report": "不是 dict",
        "podcast_brief": "有摘要",
    })
    messages = [AIMessage(content=payload)]
    display, brief = parse_event_output(messages)

    # display_report 被 isinstance(display, dict) 过滤 → None
    assert display is None
    # podcast_brief 仍正常
    assert brief == "有摘要"


def test_parse_podcast_brief_not_string():
    """podcast_brief 是数字 → 被 str() 转换"""
    payload = json.dumps({
        "display_report": {"event_title": "测试"},
        "podcast_brief": 12345,
    })
    messages = [AIMessage(content=payload)]
    display, brief = parse_event_output(messages)

    assert isinstance(display, dict)
    assert brief == "12345"


def test_parse_multiple_messages_extracts_last_ai():
    """多条消息中只解析最后一条 AIMessage"""
    payload = json.dumps({
        "display_report": {"event_title": "最终结果"},
        "podcast_brief": "最终摘要",
    })
    messages = [
        AIMessage(content='{"display_report": {"event_title": "中间结果"}}'),
        AIMessage(content=payload),
    ]
    display, brief = parse_event_output(messages)

    assert isinstance(display, dict)
    assert display["event_title"] == "最终结果"
    assert brief == "最终摘要"


def test_parse_unicode_chinese():
    """中文 JSON 内容 → 正确处理 Unicode"""
    payload = json.dumps({
        "display_report": {
            "event_title": "美国加征关税",
            "event_summary": "美国政府宣布对中国新能源汽车加征25%关税",
            "impact_direction": "negative",
        },
        "podcast_brief": "美国加征关税事件传导分析：新能源汽车产业链首当其冲",
    }, ensure_ascii=False)
    messages = [AIMessage(content=payload)]
    display, brief = parse_event_output(messages)

    assert isinstance(display, dict)
    assert display["event_title"] == "美国加征关税"
    assert "新能源汽车" in brief
