"""Brief v1 聚合服务测试。"""

import json
from unittest.mock import AsyncMock

import pytest

from aistock_agent.utils.brief_contract import (
    build_iterate_brief_summary,
    build_market_snapshot_brief_summary,
)


def _report(
    report_type: str,
    report_id: int,
    *,
    summary: str | None = None,
    created_at: str = "2026-07-24T01:00:00+00:00",
    data_source: str = "agent",
    status: str = "completed",
) -> dict[str, object]:
    return {
        "id": report_id,
        "report_type": report_type,
        "created_at": created_at,
        "data_source": data_source,
        "status": status,
        "content": {
            "display_report": {"summary": summary or f"{report_type} conclusion"},
        },
    }


def _structured_snapshot_report(
    report_id: int = 41,
    *,
    hit_rate: float = 0.65,
    new_coverage_rate: float = 0.20,
    created_at: str = "2026-07-24T07:30:00+00:00",
) -> dict[str, object]:
    """仅含可重建验证的 brief_summary.v1。"""
    snapshot = {
        "date": "2026-07-24",
        "dimension_1_coverage": {
            "hit_rate": hit_rate,
            "new_coverage_rate": new_coverage_rate,
        },
    }
    return {
        "id": report_id,
        "report_type": "market_snapshot",
        "created_at": created_at,
        "data_source": "snapshot_builder",
        "status": "completed",
        "content": {
            "brief_summary": build_market_snapshot_brief_summary(snapshot),
        },
    }


def _structured_iterate_report(
    report_id: int = 42,
    *,
    summary: str = "今日无显著异常",
    status: str = "normal",
    triggered_dimensions: list[str] | None = None,
    created_at: str = "2026-07-24T07:40:00+00:00",
) -> dict[str, object]:
    """iterate 的 LLM summary 不可直接成为 Brief 事实。"""
    payload = {
        "date": "2026-07-24",
        "status": status,
        "summary": summary,
        "triggered_dimensions": (
            triggered_dimensions
            if triggered_dimensions is not None
            else (["dimension_2"] if status == "alert" else [])
        ),
    }
    return {
        "id": report_id,
        "report_type": "iterate",
        "created_at": created_at,
        "data_source": "iterate_analyzer",
        "status": "completed",
        "content": {
            "brief_summary": build_iterate_brief_summary(payload),
        },
    }


def _structured_review_report(
    report_id: int = 31,
    *,
    summary: str = "收盘复盘结论",
    attribution_status: str = "confirmed",
    chain_nodes: list[tuple[str, str]] | None = None,
    sectors: dict[str, object] | None = None,
    created_at: str = "2026-07-24T07:00:00+00:00",
) -> dict[str, object]:
    """构造含 display_report + market_trace 的 review 报告（schema 2.0）。"""
    chain_nodes = chain_nodes or [
        ("trigger", "美联储降息预期升温"),
        ("transmission", "北向资金净流入"),
        ("observable_result", "券商领涨带动指数反弹"),
    ]
    sectors = sectors or {
        "top_gainers": [
            {"name": "AI算力", "pct_change": 4.21},
            {"name": "券商", "pct_change": 3.05},
        ],
        "top_losers": [
            {"name": "煤炭", "pct_change": -2.13},
            {"name": "银行", "pct_change": -1.52},
        ],
    }
    return {
        "id": report_id,
        "report_type": "review",
        "created_at": created_at,
        "data_source": "review_agent",
        "status": "completed",
        "content": {
            "display_report": {
                "summary": summary,
                "details": "复盘详情",
                "stocks": [],
                "sectors": [],
                "risks": [],
            },
            "schema_version": "2.0",
            "market_trace": {
                "snapshot": {
                    "a_share": {"sectors": sectors},
                    "missing_fields": [],
                },
                "trace": {
                    "attribution_status": attribution_status,
                    "primary_chain_id": "c1",
                    "candidates": [
                        {
                            "id": "c1",
                            "chain": {
                                "nodes": [
                                    {"stage": stage, "claim": claim}
                                    for stage, claim in chain_nodes
                                ]
                            },
                            "verdict": "主因确认",
                        }
                    ],
                    "unresolved_questions": [],
                },
            },
        },
    }


def _legacy_raw_json_snapshot_report(
    report_id: int = 41,
    *,
    created_at: str = "2026-07-24T07:30:00+00:00",
) -> dict[str, object]:
    """旧 schema 1.0 把完整 snapshot JSON 写入 text 字段 —— 必须被 Brief 拒绝。"""
    snapshot = {
        "date": "2026-07-24",
        "dimension_1_coverage": {"hit_rate": 0.65, "new_coverage_rate": 0.20},
    }
    return {
        "id": report_id,
        "report_type": "market_snapshot",
        "created_at": created_at,
        "data_source": "snapshot_builder",
        "status": "completed",
        "content": {
            "text": json.dumps(snapshot, ensure_ascii=False),
            "snapshot": snapshot,
        },
    }


def _legacy_raw_json_iterate_report(
    report_id: int = 42,
    *,
    created_at: str = "2026-07-24T07:40:00+00:00",
) -> dict[str, object]:
    """旧 schema 1.0 把完整 iterate JSON 写入 text 字段 —— 必须被 Brief 拒绝。"""
    payload = {
        "date": "2026-07-24",
        "status": "normal",
        "summary": "今日无显著异常",
    }
    return {
        "id": report_id,
        "report_type": "iterate",
        "created_at": created_at,
        "data_source": "iterate_analyzer",
        "status": "completed",
        "content": {"text": json.dumps(payload, ensure_ascii=False)},
    }


@pytest.mark.asyncio
async def test_morning_brief_has_three_required_items_and_real_evidence() -> None:
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "morning": _report("morning", 11, summary="morning conclusion"),
        "wind_leader": _report("wind_leader", 12),
        "hot_burst": _report("hot_burst", 13),
        "trend_score": _report("trend_score", 14),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []

    brief = await build_brief("morning", "2026-07-24", api=api)

    assert brief["schema_version"] == "brief.v1"
    assert brief["brief_type"] == "morning"
    assert brief["degraded"] is False
    assert brief["missing_sources"] == []
    assert len(brief["items"]) == 4
    first = brief["items"][0]
    assert first["conclusion"] == "morning conclusion"
    assert first["confidence"] == "unknown"
    assert first["uncertainty"] == "upstream confidence unavailable"
    assert first["evidence"] == [{
        "report_type": "morning",
        "id": "11",
        "data_source": "agent",
        "created_at": "2026-07-24T01:00:00+00:00",
    }]
    assert first["as_of"] == "2026-07-24T01:00:00+00:00"


@pytest.mark.asyncio
async def test_morning_brief_includes_trend_score_when_the_report_is_available() -> None:
    """晨间综合 Brief 应纳入当天已完成的趋势股评分，并保留真实证据来源。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "morning": _report("morning", 11),
        "wind_leader": _report("wind_leader", 12),
        "hot_burst": _report("hot_burst", 13),
        "trend_score": _report("trend_score", 14, summary="趋势股评分结论"),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []

    brief = await build_brief("morning", "2026-07-24", api=api)

    types = [item["evidence"][0]["report_type"] for item in brief["items"]]
    assert types == ["morning", "wind_leader", "hot_burst", "trend_score"]
    assert brief["missing_sources"] == []


@pytest.mark.asyncio
async def test_brief_as_of_is_anchored_to_requested_shanghai_date() -> None:
    """导入时间不能改变固定日期 Brief 的 as_of 日期。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "morning": _report("morning", 11, created_at="2026-07-23T16:00:00.000Z"),
        "wind_leader": _report("wind_leader", 12, created_at="2026-07-23T16:01:00.000Z"),
        "hot_burst": _report("hot_burst", 13, created_at="2026-07-23T16:02:00.000Z"),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []

    brief = await build_brief("morning", "2026-07-24", api=api)

    assert brief["as_of"] == "2026-07-24T00:00:00+08:00"


@pytest.mark.asyncio
async def test_morning_brief_reads_event_intent_from_persisted_event_conduction_only() -> None:
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, _date: _report(report_type, 1)
    event_newest = _report(
        "event_conduction", 20, created_at="2026-07-24T03:00:00+00:00"
    )
    event_newest["content"] = {
        "analysis_reports": {"event_podcast_brief": "最新事件结论"},
    }
    event_middle = _report(
        "event_conduction", 19, created_at="2026-07-24T02:00:00+00:00"
    )
    event_middle["content"] = {
        "analysis_reports": {"event_podcast_brief": "中间事件结论"},
    }
    event_oldest = _report(
        "event_conduction", 18, created_at="2026-07-24T01:00:00+00:00"
    )
    event_oldest["content"] = {
        "analysis_reports": {"event_podcast_brief": "较早事件结论"},
    }
    api.list_analysis_reports.return_value = [
        event_newest,
        event_middle,
        event_oldest,
    ]

    brief = await build_brief("morning", "2026-07-24", api=api)

    # 4 个 required types（morning/wind_leader/hot_burst/trend_score）+ 1 个最新有效事件 = 5 items
    assert len(brief["items"]) == 5
    event_ids = [item["evidence"][0]["id"] for item in brief["items"][4:]]
    assert event_ids == ["20"]
    api.list_analysis_reports.assert_awaited_once_with("event_conduction", "2026-07-24")


@pytest.mark.asyncio
async def test_evening_brief_is_degraded_and_names_missing_review_dimensions() -> None:
    """review 报告存在但缺 market_trace（板块/归因）时，晚报降级并标明缺失维度。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, _date: (
        _report("review", 31) if report_type == "review" else None
    )

    brief = await build_brief("evening", "2026-07-24", api=api)

    assert brief["brief_type"] == "evening"
    assert brief["degraded"] is True
    assert brief["missing_sources"] == ["review.attribution", "review.sectors"]
    assert len(brief["items"]) == 1
    assert brief["items"][0]["title"] == "收盘复盘"


@pytest.mark.asyncio
async def test_brief_marks_missing_data_source_without_fabricating_evidence() -> None:
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "morning": _report("morning", 11, data_source=""),
        "wind_leader": _report("wind_leader", 12),
        "hot_burst": _report("hot_burst", 13),
        "trend_score": _report("trend_score", 14),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []

    brief = await build_brief("morning", "2026-07-24", api=api)

    assert brief["degraded"] is True
    assert brief["missing_sources"] == ["morning"]
    assert len(brief["items"]) == 3
    assert all(item["evidence"][0]["data_source"] for item in brief["items"])


@pytest.mark.asyncio
async def test_brief_rejects_wrong_type_or_failed_persisted_rows() -> None:
    """报告类型或落库状态不可信时必须列为缺失，不能伪装成所需工件。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "morning": _report("morning", 11),
        "wind_leader": _report("hot_burst", 12),
        "hot_burst": _report("hot_burst", 13, status="failed"),
        "trend_score": _report("trend_score", 14),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []

    brief = await build_brief("morning", "2026-07-24", api=api)

    assert brief["degraded"] is True
    assert brief["missing_sources"] == ["wind_leader", "hot_burst"]
    assert [item["evidence"][0]["report_type"] for item in brief["items"]] == [
        "morning",
        "trend_score",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("report_id", [0, False, ""])
async def test_brief_rejects_untraceable_persisted_report_ids(report_id: object) -> None:
    """SERIAL 主键必须是真实正整数或非空外部字符串，不能伪造证据 ID。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "morning": _report("morning", 11),
        "wind_leader": _report("wind_leader", 12),
        "hot_burst": _report("hot_burst", 13),
        "trend_score": _report("trend_score", 14),
    }
    reports["morning"]["id"] = report_id
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []

    brief = await build_brief("morning", "2026-07-24", api=api)

    assert brief["degraded"] is True
    assert brief["missing_sources"] == ["morning"]
    assert all(item["evidence"][0]["id"] not in {"0", "False", ""} for item in brief["items"])


@pytest.mark.asyncio
async def test_morning_brief_uses_nested_event_podcast_brief_and_skips_invalid_newer_events(
) -> None:
    """事件结论来自真实嵌套 event_podcast_brief，最多选择两条有效的最新事件。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, _date: _report(report_type, 1)
    invalid_newest = _report(
        "event_conduction",
        30,
        created_at="2026-07-24T03:00:00+00:00",
    )
    invalid_newest["content"] = {
        "display_report": {"summary": "不应替代事件播报摘要的展示文本"},
        "analysis_reports": {"event_podcast_brief": ""},
    }
    valid_middle = _report(
        "event_conduction",
        29,
        created_at="2026-07-24T02:00:00+00:00",
    )
    valid_middle["content"] = {
        "analysis_reports": {"event_podcast_brief": "中间事件结论"},
    }
    valid_oldest = _report(
        "event_conduction",
        28,
        created_at="2026-07-24T01:00:00+00:00",
    )
    valid_oldest["content"] = {
        "analysis_reports": {"event_podcast_brief": "较早事件结论"},
    }
    api.list_analysis_reports.return_value = [invalid_newest, valid_middle, valid_oldest]

    brief = await build_brief("morning", "2026-07-24", api=api)

    # 4 个 required types + 最新有效事件（invalid_newest 被跳过）= 5 items
    events = brief["items"][4:]
    assert [item["evidence"][0]["id"] for item in events] == ["29"]
    assert [item["conclusion"] for item in events] == ["中间事件结论"]


@pytest.mark.asyncio
async def test_evening_brief_uses_only_same_trade_date_review_report() -> None:
    """晚报三条均从当日 review 报告提取，只查询 review 一次，归因结论放头条。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, report_date: (
        _structured_review_report(report_id=40) if report_date == "2026-07-24" else None
    )

    brief = await build_brief("evening", "2026-07-24", api=api)

    assert len(brief["items"]) == 3
    assert brief["degraded"] is False
    assert [item["title"] for item in brief["items"]] == [
        "归因结论",
        "市场快照",
        "收盘复盘",
    ]
    api.get_analysis_report.assert_awaited_once_with("review", "2026-07-24")


@pytest.mark.asyncio
async def test_persist_brief_writes_matching_brief_report_type() -> None:
    from aistock_agent.services.briefing import build_and_persist_brief

    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, _date: _report(report_type, 50)
    api.list_analysis_reports.return_value = []
    api.save_analysis_report.return_value = {"id": 99}

    saved = await build_and_persist_brief("morning", "2026-07-24", api=api)

    assert saved is True
    kwargs = api.save_analysis_report.await_args.kwargs
    assert kwargs["report_type"] == "brief_morning"
    assert kwargs["report_date"] == "2026-07-24"
    assert kwargs["data_source"] == "brief_aggregator"
    assert kwargs["status"] == "completed"
    assert kwargs["content"]["schema_version"] == "brief.v1"


# ── 契约测试：Brief item 不得携带原始 JSON conclusion ──────────────────────


def _assert_item_contract(item: dict[str, object]) -> None:
    """断言单个 Brief item 字段完整且 conclusion 不是原始 JSON。"""
    for field in ("title", "conclusion", "as_of", "confidence"):
        value = item.get(field)
        assert isinstance(value, str) and value.strip(), (
            f"Brief item 缺少非空字段 {field}"
        )
    uncertainty = item.get("uncertainty")
    assert isinstance(uncertainty, str) and uncertainty.strip() or (
        isinstance(uncertainty, list)
        and uncertainty
        and all(isinstance(u, str) and u.strip() for u in uncertainty)
    ), "Brief item uncertainty 必须是非空字符串或非空字符串列表"
    evidence = item.get("evidence")
    assert isinstance(evidence, list) and evidence, "Brief item evidence 不能为空"
    for source in evidence:
        assert isinstance(source, dict), "evidence 条目必须是 dict"
        for field in ("report_type", "id", "data_source", "created_at"):
            value = source.get(field)
            assert isinstance(value, str) and value.strip(), (
                f"evidence 缺少非空字段 {field}"
            )
    conclusion = item["conclusion"]
    assert isinstance(conclusion, str)
    stripped = conclusion.lstrip()
    assert not (stripped.startswith("{") or stripped.startswith("[")), (
        "Brief item conclusion 不得是原始 JSON 字符串"
    )


@pytest.mark.asyncio
async def test_morning_brief_items_never_carry_raw_json_conclusion() -> None:
    """晨报 Brief 每个 item 字段完整，且 conclusion 不得以 { 或 [ 开头。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "morning": _report("morning", 11, summary="今日市场震荡上行"),
        "wind_leader": _report("wind_leader", 12, summary="风口集中在新能源"),
        "hot_burst": _report("hot_burst", 13, summary="机构调研热度上升"),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []

    brief = await build_brief("morning", "2026-07-24", api=api)

    assert brief["items"]
    for item in brief["items"]:
        _assert_item_contract(item)


@pytest.mark.asyncio
async def test_evening_brief_items_never_carry_raw_json_conclusion() -> None:
    """晚报 Brief 每个 item 字段完整，且 conclusion 不得以 { 或 [ 开头。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, _date: (
        _structured_review_report(report_id=31, summary="收盘三大指数涨跌互现")
    )

    brief = await build_brief("evening", "2026-07-24", api=api)

    assert brief["degraded"] is False
    assert len(brief["items"]) == 3
    for item in brief["items"]:
        _assert_item_contract(item)


@pytest.mark.asyncio
async def test_evening_brief_shows_sector_changes_for_user() -> None:
    """市场快照条目展示当日领涨/领跌板块（用户可读），不再展示晨报迭代命中率。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, _date: _structured_review_report()

    brief = await build_brief("evening", "2026-07-24", api=api)

    sectors_item = next(item for item in brief["items"] if item["title"] == "市场快照")
    assert sectors_item["conclusion"] == (
        "今日AI算力涨4.21%、券商涨3.05%，煤炭跌2.13%、银行跌1.52%"
    )


@pytest.mark.asyncio
async def test_evening_brief_shows_attribution_chain_for_user() -> None:
    """归因结论条目展示主因链（触发→传导→结果），中文可读。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, _date: _structured_review_report()

    brief = await build_brief("evening", "2026-07-24", api=api)

    attribution_item = next(item for item in brief["items"] if item["title"] == "归因结论")
    assert attribution_item["conclusion"] == (
        "今日市场主因是美联储降息预期升温，北向资金净流入，券商领涨带动指数反弹"
    )


@pytest.mark.asyncio
async def test_evening_brief_attribution_degrades_when_not_confirmed() -> None:
    """归因未确认（not_applicable/insufficient）时，归因条目显示降级文案。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, _date: _structured_review_report(
        attribution_status="not_applicable"
    )

    brief = await build_brief("evening", "2026-07-24", api=api)

    attribution_item = next(item for item in brief["items"] if item["title"] == "归因结论")
    assert attribution_item["conclusion"] == "今日证据不足，未确认主因"


@pytest.mark.asyncio
async def test_brief_rejects_json_event_conclusion() -> None:
    """event JSON 不能绕过受控摘要进入 Brief。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "morning": _report("morning", 11, summary="晨报结论"),
        "wind_leader": _report("wind_leader", 12, summary="风口结论"),
        "hot_burst": _report("hot_burst", 13, summary="热点结论"),
    }
    raw_event = _report("event_conduction", 14)
    raw_event["content"] = {"analysis_reports": {"event_podcast_brief": '{"raw": true}'}}
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = [raw_event]

    morning = await build_brief("morning", "2026-07-24", api=api)
    assert all(
        item["evidence"][0]["report_type"] != "event_conduction"
        for item in morning["items"]
    )
