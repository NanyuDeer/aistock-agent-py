"""双层输出解析器单元测试

覆盖 parse_event_output() 的所有解析策略和降级路径。
"""

import json

import pytest
from langchain_core.messages import AIMessage

from aistock_agent.utils.output_parser import (
    _parse_json,
    parse_event_output,
    transform_to_frontend,
)


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


# ── _parse_json 测试 ──


def test_parse_json_simple_dict():
    """纯 JSON 对象 → 正确解析"""
    result = _parse_json('{"key": "value"}')
    assert isinstance(result, dict)
    assert result == {"key": "value"}


def test_parse_json_simple_list():
    """纯 JSON 数组 → 正确解析"""
    result = _parse_json('[{"a": 1}, {"b": 2}]')
    assert isinstance(result, list)
    assert len(result) == 2


def test_parse_json_markdown_code_block():
    """```json ... ``` 包裹 → 正确解析"""
    result = _parse_json('```json\n{"key": "value"}\n```')
    assert isinstance(result, dict)
    assert result == {"key": "value"}


def test_parse_json_bare_code_block():
    """``` ... ``` 包裹 → 正确解析"""
    result = _parse_json('```\n{"key": "value"}\n```')
    assert isinstance(result, dict)
    assert result == {"key": "value"}


def test_parse_json_nested_in_text():
    """JSON 嵌在文本中 → 正则匹配提取"""
    result = _parse_json('前面有文字\n{"key": "value"}\n后面也有文字')
    assert isinstance(result, dict)
    assert result == {"key": "value"}


def test_parse_json_invalid():
    """无效文本 → 返回 None"""
    result = _parse_json('这不是 JSON')
    assert result is None


def test_parse_json_empty():
    """空字符串 → 返回 None"""
    result = _parse_json('')
    assert result is None


# ── transform_to_frontend 测试 ──


def test_transform_to_frontend_full():
    """4 模块全有 → 完整映射"""
    understanding = {
        "summary": "政策延续至2027年",
        "coreChanges": [
            {"variable": "补贴预期", "before": "不确定", "after": "明确延续"}
        ]
    }
    transmission = {
        "mechanism": "补贴延续降低购车门槛",
        "variables": [
            {"name": "补贴金额", "direction": "bullish", "strength": 0.85, "explanation": "单辆最高1.5万"}
        ],
        "coreIndustry": {"name": "新能源汽车", "impact": "直接利好", "reason": "终端销量预期上调"},
        "chain": [
            {"industry": "动力电池", "relation": "上游传导", "level": 1, "direction": "bullish", "impactStrength": 0.72, "reason": "销量拉动电池需求"}
        ]
    }
    history = [
        {"historyId": "hist_001", "year": "2023", "title": "类似政策", "eventType": "产业政策", "sentiment": "bullish", "industryChange": "普涨15%", "changePercentage": 15.0}
    ]
    investment = {
        "conclusion": "新能源汽车产业链受益，中期景气改善",
        "keyPoints": ["补贴延续刺激终端需求"],
        "focusIndustries": [{"name": "新能源汽车", "direction": "positive", "reason": "直接受益"}],
        "opportunities": ["终端销量增长"],
        "risks": ["补贴依赖风险"],
        "rating": "positive"
    }
    meta = {"eventId": "evt_001", "title": "补贴延续", "source": "新华社"}

    result = transform_to_frontend(understanding, transmission, history, investment, meta)

    assert result["event_understanding"]["summary"] == "政策延续至2027年"
    assert len(result["event_understanding"]["coreChanges"]) == 1
    assert result["event_transmission"]["mechanism"] == "补贴延续降低购车门槛"
    assert result["event_transmission"]["variables"][0]["direction"] == "bullish"
    assert result["event_transmission"]["variables"][0]["strength"] == 0.85
    assert result["event_transmission"]["coreIndustry"]["name"] == "新能源汽车"
    assert len(result["event_transmission"]["chain"]) == 1
    assert result["event_transmission"]["chain"][0]["level"] == 1
    assert len(result["event_history"]) == 1
    assert result["event_history"][0]["changePercentage"] == 15.0
    assert result["event_investment"]["conclusion"] == "新能源汽车产业链受益，中期景气改善"
    assert result["event_investment"]["rating"] == "positive"


def test_transform_to_frontend_null_modules():
    """部分模块为 None → 对应位置为 None 或空数组"""
    meta = {"eventId": "evt_002", "title": "测试", "source": ""}

    result = transform_to_frontend(None, None, None, None, meta)

    assert result["event_understanding"] is None
    assert result["event_transmission"] is None
    assert result["event_history"] == []
    assert result["event_investment"] is None


def test_transform_to_frontend_chinese_direction():
    """LLM 输出中文方向值 → 正确映射为英文"""
    transmission = {
        "mechanism": "测试",
        "variables": [{"name": "x", "direction": "利好", "strength": 0.5, "explanation": ""}],
        "coreIndustry": {"name": "x", "impact": "", "reason": ""},
        "chain": [{"industry": "x", "relation": "核心行业", "level": 1, "direction": "利空", "impactStrength": 0.3, "reason": ""}]
    }
    meta = {"eventId": "evt_003", "title": "", "source": ""}

    result = transform_to_frontend({"summary": "", "coreChanges": []}, transmission, [], None, meta)

    assert result["event_transmission"]["variables"][0]["direction"] == "bullish"
    assert result["event_transmission"]["chain"][0]["direction"] == "bearish"
