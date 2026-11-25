"""完整洞察报告章节构建测试（2026-09-13；2026-09-25 结构化 blocks；2026-09-26 六阶段因果链时间轴）。

契约（app-api / 前端逐字段依赖，改动需三仓同步）：
`build_report_response(data) -> {header, sections:[{heading, blocks:[Block]}]}`

Block 判别联合（`type` 区分）：
- `kv`         : `{items:[{label, value, tone?}]}`，tone ∈ up|down（涨跌着色）
- `verdict`    : `{text, badges:[{label, value}]}`
- `candidates` : `{items:[{layer, status, verdict, evidenceIds}]}`
- `chain`      : `{stages:[{stage, stageKey, claim, epistemic, epistemicKey,
                            status, statusKey, evidenceIds, evidenceCount}]}`，**只含主链**
- `evidence`   : `{items:[{sourceId, provider, kind, occurredAt, level, title, excerpt}]}`
- `list`       : `{items:[string]}`

空节（无数据）→ `blocks: []`（由前端渲染"暂缺"）。
`*Key` 为机器可读枚举值，供前端做中性弱化判定，不参与展示。
"""

from aistock_agent.services.insight_report import (
    build_report_blocks,
    build_report_header,
    build_report_response,
)

_HEADINGS = ["事件事实", "主因结论", "分层候选归因", "六阶段因果链", "证据清单", "未解问题"]

# 主链 6 阶段（真实 artifact 的固定顺序）
_PRIMARY_NODES = [
    {"stage": "structural_root", "claim": "主力净流出而小单净流入，筹码由主力向散户转移",
     "epistemicType": "hypothesis", "status": "not_established", "evidenceIds": []},
    {"stage": "trigger", "claim": "盘中快速拉升 7.69%，触发异动阈值",
     "epistemicType": "fact", "status": "established", "evidenceIds": ["e1", "e2"]},
    {"stage": "transmission", "claim": "板块同步走强带动",
     "epistemicType": "hypothesis", "status": "not_established", "evidenceIds": []},
    {"stage": "exposure", "claim": "资金关注度提升",
     "epistemicType": "inference", "status": "partial", "evidenceIds": ["e1"]},
    {"stage": "repricing", "claim": "估值重定价",
     "epistemicType": "inference", "status": "partial", "evidenceIds": ["e1"]},
    {"stage": "observable_result", "claim": "收盘涨幅 7.69%",
     "epistemicType": "fact", "status": "established", "evidenceIds": ["e1"]},
]

_FULL_DATA = {
    "event": {
        "eventId": "mv:003018:2026-09-04:1788485647932:up",
        "symbol": "003018",
        "stockName": "金富科技",
        "tradingDate": "2026-09-04",
        "triggeredAt": "2026-09-04T01:34:07.932Z",
        "direction": "up",
        "changePct": 7.19,
        "thresholdPct": 7,
        "severity": "medium",
        "latestPrice": 60.49,
        "previousClose": 56.43,
    },
    "attribution": {
        "primaryPhrase": "液冷服务器概念板块联动",
        "confidenceLevel": "medium",
        "primaryLayer": "sector",
        "generatedAt": "2026-09-04T02:00:00Z",
        "candidates": [
            {"layer": "sector", "status": "supported", "verdict": "板块联动",
             "supportingEvidenceIds": ["e1"]},
        ],
        "chains": [
            # 备选链在前（真实数据里 role 顺序不固定），必须被忽略
            {"role": "alternative", "nodes": [
                {"stage": "trigger", "claim": "备选链触发", "epistemicType": "fact",
                 "status": "established", "evidenceIds": ["e9"]},
            ]},
            {"role": "primary", "nodes": _PRIMARY_NODES},
        ],
        "unresolvedQuestions": ["资金持续性待观察"],
        "evidenceIndex": [
            {"source_id": "e1", "kind": "news", "provider": "cls", "title": "液冷概念走强",
             "occurred_at": "2026-09-04T01:20:00Z", "source_level": "T1",
             "content_excerpt": "板块涨 3%"},
        ],
    },
}


def _blocks(data: dict) -> dict[str, list[dict]]:  # noqa: ANN001 - 测试辅助
    return {heading: blocks for heading, blocks in build_report_blocks(data)}


# ── 章节骨架 ──────────────────────────────────────────────────────────


def test_sections_contain_all_headings() -> None:
    assert [h for h, _ in build_report_blocks(_FULL_DATA)] == _HEADINGS


def test_missing_fields_produce_empty_blocks() -> None:
    """字段全缺时不抛错：每节 blocks 为空列表（前端渲染"暂缺"）。"""
    for heading, blocks in build_report_blocks({"event": {}, "attribution": {}}):
        assert blocks == [], f"{heading} 应输出空 blocks"


# ── 事件事实（kv） ────────────────────────────────────────────────────


def test_event_facts_kv_block() -> None:
    block = _blocks(_FULL_DATA)["事件事实"][0]
    assert block["type"] == "kv"
    labels = [item["label"] for item in block["items"]]
    # 页眉已有「股票名（代码）」，事件事实不再重复该行
    assert labels == ["触发时间", "方向", "涨跌幅", "严重度", "最新价", "昨收"]
    values = {item["label"]: item["value"] for item in block["items"]}
    assert values["触发时间"] == "2026-09-04 09:34"  # 已转上海时区
    assert values["方向"] == "上涨"
    assert values["涨跌幅"] == "7.19%（阈值 7%）"
    assert values["严重度"] == "中"
    assert values["最新价"] == "60.49"
    assert values["昨收"] == "56.43"
    # tone 供涨跌着色（up/down）
    assert {i["label"]: i.get("tone") for i in block["items"]}["方向"] == "up"


def test_event_facts_direction_down_tone() -> None:
    data = {"event": {**_FULL_DATA["event"], "direction": "down"}, "attribution": {}}
    items = _blocks(data)["事件事实"][0]["items"]
    assert {i["label"]: i.get("tone") for i in items}["方向"] == "down"


# ── 主因结论（verdict） ───────────────────────────────────────────────


def test_verdict_block_shape() -> None:
    block = _blocks(_FULL_DATA)["主因结论"][0]
    assert block["type"] == "verdict"
    assert block["text"] == "液冷服务器概念板块联动"
    badges = {b["label"]: b["value"] for b in block["badges"]}
    assert badges["置信度"] == "中"
    assert badges["归类标签"] == "板块层面"
    assert badges["归因生成时间"] == "2026-09-04 10:00"  # 已转上海时区


def test_verdict_text_does_not_duplicate_app_api_fallback() -> None:
    """主因正文只取 primaryPhrase（缺失即"暂缺"）。

    三级兜底 `primaryPhrase ?? primaryCandidate.verdict ?? primary_cause` 是 **app-api
    `InsightReportService.buildReportData` 的职责**，agent-py 不做重复兜底（否则两条
    兜底链会各自演化、口径漂移）。
    """
    data = {
        "event": _FULL_DATA["event"],
        "attribution": {k: v for k, v in _FULL_DATA["attribution"].items() if k != "primaryPhrase"},
    }
    assert _blocks(data)["主因结论"][0]["text"] == "暂缺"


# ── 分层候选（candidates） ────────────────────────────────────────────


def test_candidates_block_shape() -> None:
    block = _blocks(_FULL_DATA)["分层候选归因"][0]
    assert block["type"] == "candidates"
    assert block["items"][0] == {
        "layer": "板块层面",
        "status": "已佐证",
        "statusKey": "supported",
        "verdict": "板块联动",
        "evidenceIds": ["e1"],
    }


# ── 六阶段因果链（chain）—— 本次改造重点 ──────────────────────────────


def test_chain_block_only_contains_primary_chain() -> None:
    """备选链必须被剔除：只保留 role=primary 的 6 个阶段。"""
    block = _blocks(_FULL_DATA)["六阶段因果链"][0]
    assert block["type"] == "chain"
    assert len(block["stages"]) == 6
    assert "备选链触发" not in str(block)


def test_chain_stage_fields() -> None:
    stages = _blocks(_FULL_DATA)["六阶段因果链"][0]["stages"]
    first = stages[0]
    assert first["stage"] == "结构根因"
    assert first["stageKey"] == "structural_root"
    assert first["claim"] == "主力净流出而小单净流入，筹码由主力向散户转移"
    assert first["epistemic"] == "假设"
    assert first["epistemicKey"] == "hypothesis"
    assert first["status"] == "未确立"
    assert first["statusKey"] == "not_established"
    assert first["evidenceIds"] == []
    assert first["evidenceCount"] == 0
    assert stages[1]["evidenceIds"] == ["e1", "e2"]
    assert stages[1]["evidenceCount"] == 2


def test_chain_stage_order_preserved() -> None:
    stages = _blocks(_FULL_DATA)["六阶段因果链"][0]["stages"]
    assert [s["stageKey"] for s in stages] == [
        "structural_root", "trigger", "transmission",
        "exposure", "repricing", "observable_result",
    ]


def test_chain_falls_back_to_first_chain_when_no_primary() -> None:
    """真实数据均有 primary；缺失时降级取第一条链（不整节消失）。"""
    data = {
        "event": _FULL_DATA["event"],
        "attribution": {
            **_FULL_DATA["attribution"],
            "chains": [{"role": "alternative", "nodes": _PRIMARY_NODES}],
        },
    }
    assert len(_blocks(data)["六阶段因果链"][0]["stages"]) == 6


def test_chain_empty_when_no_chains() -> None:
    data = {"event": _FULL_DATA["event"], "attribution": {"chains": []}}
    assert _blocks(data)["六阶段因果链"] == []


def test_chain_skips_malformed_nodes() -> None:
    """非 dict 的节点/链被跳过，不抛错。"""
    data = {
        "event": _FULL_DATA["event"],
        "attribution": {
            **_FULL_DATA["attribution"],
            "chains": [
                "garbage",
                {"role": "primary", "nodes": ["garbage", _PRIMARY_NODES[0]]},
            ],
        },
    }
    assert len(_blocks(data)["六阶段因果链"][0]["stages"]) == 1


# ── 证据清单（evidence） ──────────────────────────────────────────────


def test_evidence_block_shape() -> None:
    item = _blocks(_FULL_DATA)["证据清单"][0]["items"][0]
    assert item["sourceId"] == "e1"
    assert item["provider"] == "财联社"  # 已中文化
    assert item["kind"] == "新闻"  # 已中文化
    assert item["occurredAt"] == "2026-09-04 09:20"  # 已转上海时区
    assert item["level"] == "T1"  # 保留原值（可溯源）
    assert item["title"] == "液冷概念走强"
    assert item["excerpt"] == "板块涨 3%"


# ── 未解问题（list） ──────────────────────────────────────────────────


def test_list_block_shape() -> None:
    block = _blocks(_FULL_DATA)["未解问题"][0]
    assert block == {"type": "list", "items": ["资金持续性待观察"]}


# ── 展示层中文化（覆盖存量英文数据） ──────────────────────────────────


def test_localize_enum_labels() -> None:
    """候选层/状态、链 stage/epistemic/status 全部中文化。"""
    data = {
        "event": _FULL_DATA["event"],
        "attribution": {
            **_FULL_DATA["attribution"],
            "candidates": [
                {"layer": "capital", "status": "insufficient", "verdict": "资金数据不足"},
            ],
            "chains": [{"role": "primary", "nodes": [{
                "stage": "structural_root", "claim": "题材归属",
                "epistemicType": "fact", "status": "not_established",
            }]}],
        },
    }
    blocks = _blocks(data)
    assert blocks["分层候选归因"][0]["items"][0]["layer"] == "资金层面"
    assert blocks["分层候选归因"][0]["items"][0]["status"] == "证据不足"
    assert blocks["分层候选归因"][0]["items"][0]["statusKey"] == "insufficient"
    stage = blocks["六阶段因果链"][0]["stages"][0]
    assert (stage["stage"], stage["epistemic"], stage["status"]) == ("结构根因", "事实", "未确立")


def test_localize_evidence_excerpt_templates() -> None:
    """存量证据里的英文摘要模板 → 中文。"""
    cases = {
        "Price change 8.23% crossed 7.00% threshold.": "涨跌幅 8.23%，突破 7.00% 阈值。",
        "Latest 26.3; previous close 24.3; change 8.23%.": "最新价 26.3；昨收 24.3；涨跌幅 8.23%。",
        "Trigger revision 2; price change 8.23%.": "触发修订 2；涨跌幅 8.23%。",
        "Board latest daily change 0.45% on 20260923.": "板块最新日涨跌幅 0.45%（2026-09-23）。",
        "上证指数 change -0.31%.": "上证指数 涨跌幅 -0.31%。",
    }
    for raw, expected in cases.items():
        data = {
            "event": _FULL_DATA["event"],
            "attribution": {
                **_FULL_DATA["attribution"],
                "evidenceIndex": [
                    {"source_id": "e1", "kind": "news", "title": "t", "content_excerpt": raw},
                ],
            },
        }
        assert _blocks(data)["证据清单"][0]["items"][0]["excerpt"] == expected


def test_localize_evidence_titles() -> None:
    data = {
        "event": _FULL_DATA["event"],
        "attribution": {
            **_FULL_DATA["attribution"],
            "evidenceIndex": [{"source_id": "e1", "kind": "trigger_fact",
                               "title": "Price trigger event", "content_excerpt": "x"}],
        },
    }
    assert _blocks(data)["证据清单"][0]["items"][0]["title"] == "价格触发事件"


def test_localize_chain_claim_free_text() -> None:
    """链节点 claim 是 LLM 自由文本，也要过中文化（字段名/compact 日期）。"""
    data = {
        "event": _FULL_DATA["event"],
        "attribution": {
            **_FULL_DATA["attribution"],
            "chains": [{"role": "primary", "nodes": [{
                "stage": "trigger",
                "claim": "快照缺失technical_context，trade_date为20260923",
                "epistemicType": "fact", "status": "partial",
            }]}],
        },
    }
    claim = _blocks(data)["六阶段因果链"][0]["stages"][0]["claim"]
    assert "技术面数据" in claim
    assert "交易日为2026-09-23" in claim
    assert "technical_context" not in claim and "trade_date" not in claim


def test_localize_candidate_verdict_free_text() -> None:
    data = {
        "event": _FULL_DATA["event"],
        "attribution": {
            **_FULL_DATA["attribution"],
            "candidates": [{"layer": "technical", "status": "insufficient",
                            "verdict": "快照缺失technical_context，无K线量价形态证据"}],
        },
    }
    verdict = _blocks(data)["分层候选归因"][0]["items"][0]["verdict"]
    assert "技术面数据" in verdict
    assert "technical_context" not in verdict


def test_unmatched_text_kept_as_is() -> None:
    """未命中映射的文本不得被改写（避免误伤正常内容）。"""
    data = {
        "event": _FULL_DATA["event"],
        "attribution": {
            **_FULL_DATA["attribution"],
            "candidates": [{"layer": "sector", "status": "supported",
                            "verdict": "纯中文描述，含 english words 但无模板"}],
        },
    }
    verdict = _blocks(data)["分层候选归因"][0]["items"][0]["verdict"]
    assert verdict == "纯中文描述，含 english words 但无模板"


# ── 页眉与响应外壳 ────────────────────────────────────────────────────


def test_build_report_header_contains_stock_and_trading_date() -> None:
    header = build_report_header(_FULL_DATA)
    assert "金富科技" in header
    assert "003018" in header
    assert "2026-09-04" in header


def test_build_report_header_tolerates_missing_fields() -> None:
    assert "暂缺" in build_report_header({"event": {}, "attribution": {}})


def test_build_report_response_shape() -> None:
    res = build_report_response(_FULL_DATA)
    assert res["header"] == "金富科技（003018） · 2026-09-04"
    assert [s["heading"] for s in res["sections"]] == _HEADINGS
    assert all("blocks" in s and "lines" not in s for s in res["sections"])
    assert res["sections"][3]["blocks"][0]["type"] == "chain"


def test_build_report_response_tolerates_missing_fields() -> None:
    res = build_report_response({"event": {}, "attribution": {}})
    assert "暂缺" in res["header"]
    assert all(s["blocks"] == [] for s in res["sections"])
