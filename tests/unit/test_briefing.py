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
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []

    brief = await build_brief("morning", "2026-07-24", api=api)

    assert brief["schema_version"] == "brief.v1"
    assert brief["brief_type"] == "morning"
    assert brief["degraded"] is False
    assert brief["missing_sources"] == []
    assert 3 <= len(brief["items"]) <= 5
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

    assert len(brief["items"]) == 5
    event_ids = [item["evidence"][0]["id"] for item in brief["items"][3:]]
    assert event_ids == ["20", "19"]
    api.list_analysis_reports.assert_awaited_once_with("event_conduction", "2026-07-24")


@pytest.mark.asyncio
async def test_evening_brief_is_degraded_and_names_missing_real_report_types() -> None:
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, _date: (
        _report("review", 31) if report_type == "review" else None
    )

    brief = await build_brief("evening", "2026-07-24", api=api)

    assert brief["brief_type"] == "evening"
    assert brief["degraded"] is True
    assert brief["missing_sources"] == ["market_snapshot", "iterate"]
    assert len(brief["items"]) == 1


@pytest.mark.asyncio
async def test_brief_marks_missing_data_source_without_fabricating_evidence() -> None:
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "morning": _report("morning", 11, data_source=""),
        "wind_leader": _report("wind_leader", 12),
        "hot_burst": _report("hot_burst", 13),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []

    brief = await build_brief("morning", "2026-07-24", api=api)

    assert brief["degraded"] is True
    assert brief["missing_sources"] == ["morning"]
    assert len(brief["items"]) == 2
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
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []

    brief = await build_brief("morning", "2026-07-24", api=api)

    assert brief["degraded"] is True
    assert brief["missing_sources"] == ["wind_leader", "hot_burst"]
    assert [item["evidence"][0]["report_type"] for item in brief["items"]] == ["morning"]


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

    events = brief["items"][3:]
    assert [item["evidence"][0]["id"] for item in events] == ["29", "28"]
    assert [item["conclusion"] for item in events] == ["中间事件结论", "较早事件结论"]


@pytest.mark.asyncio
async def test_evening_brief_uses_only_same_trade_date_reports() -> None:
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "review": _report("review", 40),
        "market_snapshot": _structured_snapshot_report(),
        "iterate": _structured_iterate_report(),
    }
    api.get_analysis_report.side_effect = lambda report_type, report_date: (
        reports.get(report_type) if report_date == "2026-07-24" else None
    )

    brief = await build_brief("evening", "2026-07-24", api=api)

    assert len(brief["items"]) == 3
    assert brief["degraded"] is False
    assert [call.args[1] for call in api.get_analysis_report.await_args_list] == [
        "2026-07-24",
        "2026-07-24",
        "2026-07-24",
    ]


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
    reports = {
        "review": _report("review", 31, summary="收盘三大指数涨跌互现"),
        "market_snapshot": _structured_snapshot_report(),
        "iterate": _structured_iterate_report(),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)

    brief = await build_brief("evening", "2026-07-24", api=api)

    assert brief["degraded"] is False
    assert len(brief["items"]) == 3
    for item in brief["items"]:
        _assert_item_contract(item)


@pytest.mark.asyncio
async def test_evening_brief_reads_summary_from_structured_market_snapshot() -> None:
    """schema 2.0 持久化的 market_snapshot，Brief conclusion 必须是可读 summary。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "review": _report("review", 31, summary="收盘复盘结论"),
        "market_snapshot": _structured_snapshot_report(hit_rate=0.72, new_coverage_rate=0.15),
        "iterate": _structured_iterate_report(summary="今日无显著异常"),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)

    brief = await build_brief("evening", "2026-07-24", api=api)

    snapshot_item = next(
        item for item in brief["items"]
        if item["evidence"][0]["report_type"] == "market_snapshot"
    )
    conclusion = snapshot_item["conclusion"]
    assert isinstance(conclusion, str)
    assert "板块命中率" in conclusion and "新覆盖率" in conclusion
    assert not conclusion.lstrip().startswith("{")


@pytest.mark.asyncio
async def test_evening_brief_reads_summary_from_structured_iterate() -> None:
    """schema 2.0 持久化的 iterate，Brief conclusion 必须是 iterate_payload.summary。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    iterate_summary = "模型伪造摘要，不得进入 Brief"
    reports = {
        "review": _report("review", 31, summary="收盘复盘结论"),
        "market_snapshot": _structured_snapshot_report(),
        "iterate": _structured_iterate_report(summary=iterate_summary, status="alert"),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)

    brief = await build_brief("evening", "2026-07-24", api=api)

    iterate_item = next(
        item for item in brief["items"]
        if item["evidence"][0]["report_type"] == "iterate"
    )
    assert iterate_item["conclusion"] == "检测到异常维度：dimension_2"


@pytest.mark.asyncio
async def test_evening_brief_degrades_when_market_snapshot_persisted_as_raw_json() -> None:
    """旧 schema 1.0 把完整 snapshot JSON 写入 text —— Brief 必须降级，不得当作 conclusion。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "review": _report("review", 31, summary="收盘复盘结论"),
        "market_snapshot": _legacy_raw_json_snapshot_report(),
        "iterate": _structured_iterate_report(),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)

    brief = await build_brief("evening", "2026-07-24", api=api)

    assert brief["degraded"] is True
    assert "market_snapshot" in brief["missing_sources"]
    types_in_items = [
        item["evidence"][0]["report_type"] for item in brief["items"]
    ]
    assert "market_snapshot" not in types_in_items
    for item in brief["items"]:
        _assert_item_contract(item)


@pytest.mark.asyncio
async def test_evening_brief_degrades_when_iterate_persisted_as_raw_json() -> None:
    """旧 schema 1.0 把完整 iterate JSON 写入 text —— Brief 必须降级，不得当作 conclusion。"""
    from aistock_agent.services.briefing import build_brief

    api = AsyncMock()
    reports = {
        "review": _report("review", 31, summary="收盘复盘结论"),
        "market_snapshot": _structured_snapshot_report(),
        "iterate": _legacy_raw_json_iterate_report(),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)

    brief = await build_brief("evening", "2026-07-24", api=api)

    assert brief["degraded"] is True
    assert "iterate" in brief["missing_sources"]
    types_in_items = [
        item["evidence"][0]["report_type"] for item in brief["items"]
    ]
    assert "iterate" not in types_in_items


@pytest.mark.asyncio
async def test_brief_rejects_json_event_conclusion_and_special_source_fallbacks() -> None:
    """event JSON、snapshot/iterate 的 display/details 均不能绕过受控摘要。"""
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

    special_content = {
        "display_report": {"summary": "伪摘要", "details": "完整详情"},
        "snapshot": {"dimension_1_coverage": {"hit_rate": 0.9}},
        "iterate_payload": {"status": "normal", "triggered_dimensions": []},
    }
    reports.update(
        {
            "review": _report("review", 31, summary="复盘结论"),
            "market_snapshot": _report("market_snapshot", 32, summary="ignored"),
            "iterate": _report("iterate", 33, summary="ignored"),
        }
    )
    reports["market_snapshot"]["content"] = special_content
    reports["iterate"]["content"] = special_content
    evening = await build_brief("evening", "2026-07-24", api=api)
    assert set(evening["missing_sources"]) == {"market_snapshot", "iterate"}
