"""双层输出解析器单元测试

覆盖 parse_event_output() 的所有解析策略和降级路径。
"""

import json

from langchain_core.messages import AIMessage

from aistock_agent.utils.output_parser import (
    _parse_json,
    extract_major_events,
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
            {
                "name": "补贴金额",
                "direction": "bullish",
                "strength": 0.85,
                "explanation": "单辆最高1.5万",
            }
        ],
        "coreIndustry": {"name": "新能源汽车", "impact": "直接利好", "reason": "终端销量预期上调"},
        "chain": [
            {
                "industry": "新能源汽车",
                "relation": "核心行业",
                "level": 1,
                "direction": "bullish",
                "impactStrength": 0.72,
                "reason": "事件变量推断",
            }
        ]
    }
    history = [
        {
            "historyId": "hist_001",
            "year": "2023",
            "title": "类似政策",
            "eventType": "产业政策",
            "sentiment": "bullish",
            "industryChange": "普涨15%",
            "changePercentage": 15.0,
        }
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
        "chain": [
            {
                "industry": "x",
                "relation": "核心行业",
                "level": 1,
                "direction": "利空",
                "impactStrength": 0.3,
                "reason": "",
            }
        ]
    }
    meta = {"eventId": "evt_003", "title": "", "source": ""}

    result = transform_to_frontend({"summary": "", "coreChanges": []}, transmission, [], None, meta)

    assert result["event_transmission"]["variables"][0]["direction"] == "bullish"
    assert result["event_transmission"]["chain"][0]["direction"] == "bearish"


def test_transform_to_frontend_constrains_chain_to_found_industry_graph() -> None:
    """成功图谱只允许一跳邻接行业，并明确它们不是确定因果。"""
    transmission = {
        "mechanism": "测试",
        "variables": [],
        "coreIndustry": {"name": "半导体", "impact": "", "reason": ""},
        "chain": [
            {
                "industry": "半导体",
                "relation": "核心行业",
                "level": 1,
                "direction": "bullish",
                "impactStrength": 0.8,
                "reason": "核心行业的事件变量推断",
            },
            {
                "industry": "伪造核心行业",
                "relation": "核心行业",
                "level": 1,
                "direction": "bullish",
                "impactStrength": 0.9,
                "reason": "模型补造",
            },
            {
                "industry": "电子化学品",
                "relation": "上游传导",
                "level": 3,
                "direction": "bearish",
                "impactStrength": 0.6,
                "reason": "上下游关系证明其必然导致成本上涨",
            },
            {
                "industry": "计算机设备",
                "relation": "下游传导",
                "level": 4,
                "direction": "bullish",
                "impactStrength": 0.5,
                "reason": "上下游关系证明其必然导致成本上涨",
            },
            {
                "industry": "虚构行业",
                "relation": "下游传导",
                "level": 2,
                "direction": "bullish",
                "impactStrength": 0.9,
                "reason": "模型补造",
            },
        ],
        "industryGraphEvidence": [
            {
                "status": "found",
                "degraded": False,
                "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {"id": "881121.TI", "name": "半导体"},
                "upstream": [
                    {"id": "881201.TI", "name": "电子化学品", "leadingStocks": []}
                ],
                "downstream": [
                    {"id": "881301.TI", "name": "计算机设备", "leadingStocks": []}
                ],
                "graphVersion": "kg-2026-07-22",
                "updatedAt": "2026-07-22T09:00:00Z",
                "missingBoundary": None,
            }
        ],
    }

    result = transform_to_frontend(
        {"summary": "测试", "coreChanges": []},
        transmission,
        [],
        None,
        {"eventId": "evt_kg", "title": "测试", "source": ""},
    )

    mapped = result["event_transmission"]
    assert mapped["industryGraphEvidence"][0]["status"] == "found"
    chain = mapped["chain"]
    assert [item["industry"] for item in chain] == ["半导体"]
    assert [item["relation"] for item in chain] == ["核心行业"]
    assert [item["level"] for item in chain] == [1]
    assert chain[0]["reason"] == "核心行业的事件变量推断"


def test_transform_to_frontend_multi_center_no_cross_chain_contamination() -> None:
    """多中心行业：邻接行业只能挂在它真正直接相邻的中心行业下，不得串链。

    场景：两个中心行业 半导体 / 汽车，各自有独立的上下游。
    模型把汽车的上游"钢铁"放在半导体段、把半导体的下游"计算机设备"放在汽车段。
    全局合并的旧实现会把它们误标为"图谱上游/下游"，串链；正确行为是丢弃这些
    没有对应中心证据支持的链节。
    """
    transmission = {
        "mechanism": "测试",
        "variables": [],
        "coreIndustry": {"name": "半导体", "impact": "", "reason": ""},
        "chain": [
            {
                "industry": "半导体",
                "relation": "核心行业",
                "level": 1,
                "direction": "bullish",
                "impactStrength": 0.8,
                "reason": "核心行业",
            },
            # 半导体的真实上游 → 保留
            {
                "industry": "电子化学品",
                "relation": "上游传导",
                "level": 2,
                "direction": "bearish",
                "impactStrength": 0.6,
                "reason": "半导体上游",
            },
            # 钢铁只是汽车的上游，挂在半导体段下 → 必须丢弃（串链）
            {
                "industry": "钢铁",
                "relation": "上游传导",
                "level": 2,
                "direction": "bearish",
                "impactStrength": 0.6,
                "reason": "汽车上游被错挂到半导体",
            },
            {
                "industry": "汽车",
                "relation": "核心行业",
                "level": 1,
                "direction": "bullish",
                "impactStrength": 0.7,
                "reason": "核心行业",
            },
            # 计算机设备只是半导体的下游，挂在汽车段下 → 必须丢弃（串链）
            {
                "industry": "计算机设备",
                "relation": "下游传导",
                "level": 2,
                "direction": "bullish",
                "impactStrength": 0.5,
                "reason": "半导体下游被错挂到汽车",
            },
            # 汽车的真实下游 → 保留
            {
                "industry": "汽车零部件",
                "relation": "下游传导",
                "level": 2,
                "direction": "bullish",
                "impactStrength": 0.5,
                "reason": "汽车下游",
            },
        ],
        "industryGraphEvidence": [
            {
                "status": "found",
                "degraded": False,
                "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {"id": "881121.TI", "name": "半导体"},
                "upstream": [
                    {"id": "881201.TI", "name": "电子化学品", "leadingStocks": []}
                ],
                "downstream": [
                    {"id": "881301.TI", "name": "计算机设备", "leadingStocks": []}
                ],
                "missingBoundary": None,
            },
            {
                "status": "found",
                "degraded": False,
                "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {"id": "881161.TI", "name": "汽车"},
                "upstream": [
                    {"id": "881211.TI", "name": "钢铁", "leadingStocks": []}
                ],
                "downstream": [
                    {"id": "881361.TI", "name": "汽车零部件", "leadingStocks": []}
                ],
                "missingBoundary": None,
            },
        ],
    }

    result = transform_to_frontend(
        {"summary": "测试", "coreChanges": []},
        transmission,
        [],
        None,
        {"eventId": "evt_multi", "title": "测试", "source": ""},
    )

    chain = result["event_transmission"]["chain"]
    # 仅保留有对应中心证据支持的链节：钢铁/计算机设备的错挂链节必须被丢弃。
    assert [item["industry"] for item in chain] == [
        "半导体",
        "电子化学品",
        "汽车",
        "汽车零部件",
    ]
    assert [item["relation"] for item in chain] == [
        "核心行业",
        "图谱上游（直接关系）",
        "核心行业",
        "图谱下游（直接关系）",
    ]
    # 显式断言串链行业不会出现
    industry_names = {item["industry"] for item in chain}
    assert "钢铁" not in industry_names, "汽车上游钢铁不得串到半导体段"
    assert "计算机设备" not in industry_names, "半导体下游计算机设备不得串到汽车段"


def test_transform_to_frontend_multi_center_same_name_direction_conflict() -> None:
    """上下游同名冲突：同一行业名是一个中心的上游、另一个中心的下游。

    场景：铜是半导体的上游、家电的下游。模型在半导体段把铜标为上游传导、
    在家电段把铜标为下游传导。旧实现把上下游合并成全局集合后会两次都标成
    "图谱上游"（if/elif 命中上游分支），丢失家电段的下游关系；正确行为是
    按各自中心证据分别标注方向，不串链。
    """
    transmission = {
        "mechanism": "测试",
        "variables": [],
        "coreIndustry": {"name": "半导体", "impact": "", "reason": ""},
        "chain": [
            {
                "industry": "半导体",
                "relation": "核心行业",
                "level": 1,
                "direction": "bullish",
                "impactStrength": 0.8,
                "reason": "核心行业",
            },
            {
                "industry": "铜",
                "relation": "上游传导",
                "level": 2,
                "direction": "bearish",
                "impactStrength": 0.6,
                "reason": "半导体上游铜",
            },
            {
                "industry": "家电",
                "relation": "核心行业",
                "level": 1,
                "direction": "bullish",
                "impactStrength": 0.7,
                "reason": "核心行业",
            },
            {
                "industry": "铜",
                "relation": "下游传导",
                "level": 2,
                "direction": "bullish",
                "impactStrength": 0.5,
                "reason": "家电下游铜",
            },
        ],
        "industryGraphEvidence": [
            {
                "status": "found",
                "degraded": False,
                "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {"id": "881121.TI", "name": "半导体"},
                "upstream": [
                    {"id": "881221.TI", "name": "铜", "leadingStocks": []}
                ],
                "downstream": [],
                "missingBoundary": None,
            },
            {
                "status": "found",
                "degraded": False,
                "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {"id": "881181.TI", "name": "家电"},
                "upstream": [],
                "downstream": [
                    {"id": "881221.TI", "name": "铜", "leadingStocks": []}
                ],
                "missingBoundary": None,
            },
        ],
    }

    result = transform_to_frontend(
        {"summary": "测试", "coreChanges": []},
        transmission,
        [],
        None,
        {"eventId": "evt_conflict", "title": "测试", "source": ""},
    )

    chain = result["event_transmission"]["chain"]
    # 两个"铜"都保留，但方向按各自中心证据分别标注，不串链。
    assert [item["industry"] for item in chain] == ["半导体", "铜", "家电", "铜"]
    assert [item["relation"] for item in chain] == [
        "核心行业",
        "图谱上游（直接关系）",
        "核心行业",
        "图谱下游（直接关系）",
    ]


def test_transform_to_frontend_rejects_found_evidence_from_non_industry_kg_source() -> None:
    """found 证据来源不是 IndustryKGService 时必须降级为无效响应。"""
    transmission = {
        "mechanism": "测试",
        "variables": [],
        "coreIndustry": {"name": "半导体", "impact": "", "reason": ""},
        "chain": [],
        "industryGraphEvidence": [{
            "status": "found",
            "degraded": False,
            "scope": "one_hop",
            "source": "OtherIndustryService",
            "industry": {"id": "881121.TI", "name": "半导体"},
            "upstream": [],
            "downstream": [],
        }],
    }

    result = transform_to_frontend(
        {"summary": "测试", "coreChanges": []}, transmission, [], None,
        {"eventId": "evt_source", "title": "测试", "source": ""},
    )

    evidence = result["event_transmission"]["industryGraphEvidence"]
    assert evidence[0]["status"] == "invalid_response"
    assert evidence[0]["source"] is None


def test_transform_to_frontend_rejects_found_evidence_with_malformed_nodes() -> None:
    """畸形图谱节点不能支持模型的上下游链节。"""
    transmission = {
        "mechanism": "测试",
        "variables": [],
        "coreIndustry": {"name": "半导体", "impact": "", "reason": ""},
        "chain": [
            {
                "industry": "伪造行业",
                "relation": "上游传导",
                "level": 2,
                "direction": "bullish",
                "impactStrength": 0.8,
                "reason": "模型补造",
            }
        ],
        "industryGraphEvidence": [
            {
                "status": "found",
                "degraded": False,
                "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {},
                "upstream": [{"name": "伪造行业"}],
                "downstream": [],
            }
        ],
    }

    result = transform_to_frontend(
        {"summary": "测试", "coreChanges": []},
        transmission,
        [],
        None,
        {"eventId": "evt_forged", "title": "测试", "source": ""},
    )

    mapped = result["event_transmission"]
    assert mapped["industryGraphEvidence"][0]["status"] == "invalid_response"
    assert mapped["chain"] == []


def test_transform_to_frontend_clears_stale_center_after_unverified_core_industry() -> None:
    """未验证核心必须清空前一中心，后续邻接不得继续挂接。"""
    transmission = {
        "mechanism": "测试",
        "variables": [],
        "coreIndustry": {"name": "半导体", "impact": "", "reason": ""},
        "chain": [
            {"industry": "半导体", "relation": "核心行业", "level": 1},
            {"industry": "无证据核心", "relation": "核心行业", "level": 1},
            {"industry": "电子化学品", "relation": "上游传导", "level": 2},
        ],
        "industryGraphEvidence": [
            {
                "status": "found", "degraded": False, "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {"id": "semi", "name": "半导体"},
                "upstream": [{"id": "chem", "name": "电子化学品", "leadingStocks": []}],
                "downstream": [],
            }
        ],
    }

    result = transform_to_frontend(
        {"summary": "测试", "coreChanges": []}, transmission, [], None,
        {"eventId": "evt_stale", "title": "测试", "source": ""},
    )

    assert [item["industry"] for item in result["event_transmission"]["chain"]] == ["半导体"]


def test_transform_to_frontend_rejects_ambiguous_same_name_centers() -> None:
    """同名中心映射到多个 ID 时必须 fail-closed。"""
    transmission = {
        "mechanism": "测试",
        "variables": [],
        "coreIndustry": {"name": "新能源", "impact": "", "reason": ""},
        "chain": [
            {"industry": "新能源", "relation": "核心行业", "level": 1},
            {"industry": "锂矿", "relation": "上游传导", "level": 2},
        ],
        "industryGraphEvidence": [
            {
                "status": "found", "degraded": False, "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {"id": "new-energy-a", "name": "新能源"},
                "upstream": [{"id": "lithium", "name": "锂矿", "leadingStocks": []}],
                "downstream": [],
            },
            {
                "status": "found", "degraded": False, "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {"id": "new-energy-b", "name": "新能源"},
                "upstream": [{"id": "copper", "name": "铜矿", "leadingStocks": []}],
                "downstream": [],
            },
        ],
    }

    result = transform_to_frontend(
        {"summary": "测试", "coreChanges": []}, transmission, [], None,
        {"eventId": "evt_ambiguous", "title": "测试", "source": ""},
    )

    assert result["event_transmission"]["chain"] == []


# ── extract_major_events 容错解析测试（生产可靠性修复） ──


def _major_events_text(events_raw: str) -> str:
    """构造带 MAJOR_EVENTS 标记的晨报 details 文本（events_raw 为原始 JSON 块文本）。"""
    return (
        "## 重大事件识别\n\n"
        "<!--MAJOR_EVENTS_START-->\n"
        f"{events_raw}\n"
        "<!--MAJOR_EVENTS_END-->\n"
    )


def _valid_event(title: str, **overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "title": title,
        "summary": f"{title}摘要",
        "url": f"https://example.com/{title}",
        "impact_score": 4.0,
        "direction": "positive",
        "involved_keywords": ["关键词"],
    }
    event.update(overrides)
    return event


def test_extract_major_events_full_valid_json():
    """Case1：正常 JSON → 全量解析成功（回归保护）"""
    events = [_valid_event(f"事件{i}") for i in range(1, 7)]
    text = _major_events_text(json.dumps(events, ensure_ascii=False))

    result = extract_major_events(text)

    assert len(result) == 6
    assert [e["title"] for e in result] == [f"事件{i}" for i in range(1, 7)]


def test_extract_major_events_recovers_others_when_one_summary_has_unescaped_quotes():
    """Case2：1 个事件 summary 含未转义中文引号 → 恢复其余 5 个，不允许返回空数组

    复现生产故障：summary 中出现 `"小非农"` 这类未转义引号时，
    旧的整块 json.loads 会抛 JSONDecodeError 并返回空数组，导致全量事件丢失。
    """
    raw = """[
  {"title": "事件1", "summary": "事件1摘要", "url": "https://e.com/1", "impact_score": 4.5, "direction": "positive", "involved_keywords": ["a"]},
  {"title": "事件2", "summary": "事件2摘要", "url": "https://e.com/2", "impact_score": 4.0, "direction": "negative", "involved_keywords": ["b"]},
  {"title": "事件3", "summary": "事件3摘要", "url": "https://e.com/3", "impact_score": 3.5, "direction": "positive", "involved_keywords": ["c"]},
  {"title": "事件4", "summary": "市场称其为"小非农"数据，该数据大幅不及预期", "url": "https://e.com/4", "impact_score": 3.0, "direction": "positive", "involved_keywords": ["d"]},
  {"title": "事件5", "summary": "事件5摘要", "url": "https://e.com/5", "impact_score": 3.0, "direction": "negative", "involved_keywords": ["e"]},
  {"title": "事件6", "summary": "事件6摘要", "url": "https://e.com/6", "impact_score": 2.5, "direction": "positive", "involved_keywords": ["f"]}
]"""
    text = _major_events_text(raw)

    result = extract_major_events(text)

    assert len(result) == 5, f"期望恢复 5 个事件，实际返回 {len(result)}"
    titles = [e["title"] for e in result]
    assert "事件4" not in titles, "含未转义引号的事件应被丢弃"
    assert "事件1" in titles and "事件6" in titles


def test_extract_major_events_skips_events_missing_required_fields():
    """Case3：单个事件字段缺失（title 为空 / direction 非法）→ 跳过坏事件，保留其他事件"""
    events = [
        _valid_event("好事件1"),
        {
            "title": "",
            "summary": "缺标题事件",
            "url": "",
            "impact_score": 3.0,
            "direction": "positive",
            "involved_keywords": [],
        },
        _valid_event("好事件2", direction="neutral"),
    ]
    text = _major_events_text(json.dumps(events, ensure_ascii=False))

    result = extract_major_events(text)

    assert [e["title"] for e in result] == ["好事件1"]


def test_extract_major_events_normalizes_fields():
    """字段校验：impact_score 转 float、direction 大小写归一、involved_keywords 默认空数组、url 允许空"""
    events = [
        {
            "title": "事件",
            "summary": "摘要",
            "url": "",
            "impact_score": "4.5",
            "direction": "Positive",
        }
    ]
    text = _major_events_text(json.dumps(events, ensure_ascii=False))

    result = extract_major_events(text)

    assert len(result) == 1
    event = result[0]
    assert event["impact_score"] == 4.5
    assert isinstance(event["impact_score"], float)
    assert event["direction"] == "positive"
    assert event["url"] == ""
    assert event["involved_keywords"] == []


def test_extract_major_events_legacy_json_array_without_marker():
    """无标记块但存在 JSON 数组 → 兼容路径正常解析（回归保护）"""
    text = (
        "以下是重大事件：\n"
        '[{"title": "事件A", "summary": "摘要A", "url": "", '
        '"impact_score": 3.0, "direction": "positive", "involved_keywords": []}]\n'
        "以上。"
    )

    result = extract_major_events(text)

    assert len(result) == 1
    assert result[0]["title"] == "事件A"


def test_extract_major_events_no_marker_returns_empty():
    """无标记块也无 JSON 数组 → 返回空数组（回归保护）"""
    assert extract_major_events("这是一段纯文本晨报，没有事件标记。") == []
