"""market_trace_qa 服务单元测试。

验证：
- 只消费已持久化且校验通过的 ReviewArtifact
- LLM 草稿仅输出 answer + source_ids，服务端验证 source_ids
- 各种失败路径返回 degraded=true 响应
- 不编造结论（无当日复盘时返回降级）
"""

import json
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from aistock_agent.schemas.market_trace import (
    MarketTraceResult,
    MarketTraceSnapshot,
    SourceRecord,
)
from aistock_agent.services import market_trace_qa as market_trace_qa_service
from aistock_agent.services.data_client import ReviewReportReadResult
from aistock_agent.services.market_trace_qa import (
    _candidate_source_ids,
    _MarketTraceQaSelection,
    _render_selection,
    answer_market_trace_qa,
)
from aistock_agent.services.phenomenon_discovery import discover_market_phenomenon

# ============================================================================
# 测试数据 - 复用 review_agent 测试的数据结构
# ============================================================================

_CAPTURED_AT = datetime(2026, 7, 17, 15, 30, tzinfo=UTC)
_TRADE_DATE = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)
_REPORT_DATE = "2026-07-17"


def _found_review_report(report: dict[str, object]) -> ReviewReportReadResult:
    return ReviewReportReadResult("found", report)


def _make_source(source_id: str, **overrides: object) -> SourceRecord:
    defaults: dict[str, object] = {
        "source_id": source_id,
        "kind": "market_fact",
        "provider": "test",
        "title": source_id,
        "content": "test content",
        "url": None,
        "occurred_at": _TRADE_DATE,
        "captured_at": _CAPTURED_AT,
        "source_level": "market_data",
    }
    defaults.update(overrides)
    return SourceRecord(**defaults)  # type: ignore[arg-type]


_A_SHARE: dict[str, object] = {
    "indexes": {
        "SH000001": {"ts_code": "000001.SH", "pct_chg": 1.2},
        "SZ399001": {"ts_code": "399001.SZ", "pct_chg": 1.5},
        "SZ399006": {"ts_code": "399006.SZ", "pct_chg": 1.8},
        "SH000300": {"ts_code": "000300.SH", "pct_chg": 1.0},
        "SH000905": {"ts_code": "000905.SH", "pct_chg": 0.9},
        "SH000852": {"ts_code": "000852.SH", "pct_chg": 1.1},
    },
    "breadth": {"advance_ratio": 0.75, "total_count": 5000, "decline_count": 1000},
    "turnover": {"change_pct": 15.0},
    "limits": {"up_count": 50, "down_count": 10, "broken_count": 5, "highest_board": 3},
    "main_force": {"large_and_extra_large_net_yuan": 5_000_000_000},
    "sectors": {
        "top_gainers": [{"name": "半导体"}],
        "top_losers": [{"name": "房地产"}],
    },
}

_SOURCES = {
    "INDEX_000001_SH": _make_source(
        "INDEX_000001_SH",
        provider="tushare:index_daily",
        title="上证指数",
        content="close=3200.0, pct_chg=0.5",
    ),
    "INDEX_399001_SZ": _make_source("INDEX_399001_SZ"),
    "INDEX_399006_SZ": _make_source("INDEX_399006_SZ"),
    "INDEX_000300_SH": _make_source("INDEX_000300_SH"),
    "INDEX_000905_SH": _make_source("INDEX_000905_SH"),
    "INDEX_000852_SH": _make_source("INDEX_000852_SH"),
    "BREADTH_ALL": _make_source("BREADTH_ALL"),
    "TURNOVER_ALL": _make_source("TURNOVER_ALL"),
    "LIMITS_ALL": _make_source("LIMITS_ALL"),
    "MAIN_FORCE_ALL": _make_source("MAIN_FORCE_ALL"),
    "SECTORS_ALL": _make_source("SECTORS_ALL"),
    "NEWS_001": _make_source(
        "NEWS_001",
        kind="event_evidence",
        provider="cls",
        title="央行宣布降准",
        content="中国人民银行决定下调存款准备金率0.5个百分点",
        url="https://www.cls.cn/news/1",
        source_level="reporting",
    ),
    "GLOBAL_001": _make_source(
        "GLOBAL_001",
        provider="yfinance",
        title="标普500",
        content="price=5500.0, change_pct=0.36",
    ),
    "SEARCH_001": _make_source(
        "SEARCH_001",
        kind="event_evidence",
        provider="tavily",
        title="美联储维持利率不变",
        content="美联储在最新议息会议上决定维持联邦基金利率目标区间不变",
        url="https://example.com/fed",
        source_level="reporting",
    ),
}

SNAPSHOT = MarketTraceSnapshot(
    snapshot_id="trace-20260717",
    trade_date=_REPORT_DATE,
    captured_at=_CAPTURED_AT,
    a_share=_A_SHARE,
    sources=_SOURCES,
    missing_fields=[],
    phenomenon_discovery=discover_market_phenomenon(_A_SHARE, _SOURCES, _CAPTURED_AT, []),
)


def _chain_nodes() -> list[dict[str, object]]:
    return [
        {"stage": "structural_root", "claim": "国内货币政策宽松周期", "evidence_ids": ["NEWS_001"]},
        {"stage": "trigger", "claim": "央行宣布降准0.5个百分点", "evidence_ids": ["NEWS_001"]},
        {
            "stage": "transmission",
            "claim": "银行间流动性宽松传导至权益",
            "evidence_ids": ["NEWS_001"],
        },
        {"stage": "exposure", "claim": "金融板块直接受益", "evidence_ids": ["INDEX_000001_SH"]},
        {"stage": "repricing", "claim": "市场情绪回暖", "evidence_ids": ["INDEX_000001_SH"]},
        {
            "stage": "observable_result",
            "claim": "上证指数上涨0.5%",
            "evidence_ids": ["INDEX_000001_SH"],
        },
    ]


def _alt_chain_nodes() -> list[dict[str, object]]:
    return [
        {"stage": "structural_root", "claim": "美联储维持利率", "evidence_ids": ["SEARCH_001"]},
        {"stage": "trigger", "claim": "全球流动性宽松预期", "evidence_ids": ["GLOBAL_001"]},
        {"stage": "transmission", "claim": "外资流入新兴市场", "evidence_ids": ["GLOBAL_001"]},
        {"stage": "exposure", "claim": "北向资金净流入", "evidence_ids": ["INDEX_000001_SH"]},
        {"stage": "repricing", "claim": "权重股估值抬升", "evidence_ids": ["INDEX_000001_SH"]},
        {
            "stage": "observable_result",
            "claim": "上证指数上涨0.5%",
            "evidence_ids": ["INDEX_000001_SH"],
        },
    ]


VALID_TRACE_DICT: dict[str, object] = {
    "schema_version": "1.1",
    "attribution_status": "confirmed",
    "candidates": [
        {
            "id": "global_risk_liquidity",
            "category": "global_risk_liquidity",
            "status": "weak",
            "verdict": "全球风险偏好改善但非主因",
            "chain": {"nodes": _alt_chain_nodes()},
            "supporting_evidence_ids": ["GLOBAL_001", "SEARCH_001"],
            "counter_evidence_ids": [],
        },
        {
            "id": "domestic_macro_policy",
            "category": "domestic_macro_policy",
            "status": "supported",
            "verdict": "央行降准释放流动性是主因",
            "chain": {"nodes": _chain_nodes()},
            "supporting_evidence_ids": ["NEWS_001", "INDEX_000001_SH"],
            "counter_evidence_ids": [],
        },
        {
            "id": "industry_technology_supply",
            "category": "industry_technology_supply",
            "status": "insufficient",
            "verdict": "无明确产业供给冲击",
            "chain": None,
            "supporting_evidence_ids": [],
            "counter_evidence_ids": [],
        },
        {
            "id": "market_positioning_liquidity",
            "category": "market_positioning_liquidity",
            "status": "rejected",
            "verdict": "市场定位与流动性非独立驱动因素",
            "chain": None,
            "supporting_evidence_ids": [],
            "counter_evidence_ids": ["INDEX_000001_SH"],
        },
    ],
    "primary_chain_id": "domestic_macro_policy",
    "alternative_chain_id": "global_risk_liquidity",
    "confidence": "high",
    "unresolved_questions": ["降准对银行净息差的长期影响尚不明确"],
}


def _make_report_content() -> dict[str, object]:
    """构造持久化的 review 报告 content（schema v2）。"""
    return {
        "display_report": {
            "summary": "央行降准释放流动性",
            "details": "## 主导现象\n...",
            "stocks": [],
            "sectors": ["半导体"],
            "risks": ["降准对银行净息差的长期影响尚不明确"],
        },
        "podcast_brief": "",
        "schema_version": "2.0",
        "snapshot_id": "trace-20260717",
        "market_trace": {
            "snapshot": SNAPSHOT.model_dump(mode="json"),
            "trace": VALID_TRACE_DICT,
        },
    }


def _make_llm_response(
    answer_type: str,
    source_ids: list[str],
    candidate_id: str | None = None,
    phenomenon_kind: str | None = None,
) -> AIMessage:
    selection: dict[str, object] = {
        "answer_type": answer_type,
        "candidate_id": candidate_id,
        "phenomenon_kind": phenomenon_kind,
        "source_ids": source_ids,
    }
    return AIMessage(content=json.dumps(selection))


# ============================================================================
# 测试用例
# ============================================================================


@pytest.mark.asyncio
async def test_happy_path_returns_structured_response_with_trace():
    """正常路径：有效报告 + 有效 LLM 输出 -> 结构化响应。"""
    report = {
        "id": "report-artifact-20260717",
        "content": _make_report_content(),
        "status": "completed",
    }
    llm_resp = _make_llm_response(
        "candidate", ["INDEX_000001_SH", "NEWS_001"], "domestic_macro_policy"
    )

    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
        ),
    ):
        result = await answer_market_trace_qa("大盘为何涨跌", _REPORT_DATE, "mtqa_test")

    assert result.content == (
        "复盘候选（已支持）：央行降准释放流动性是主因。"
        "这是已归档复盘中的证据归因，不等同于确认因果关系。"
    )
    assert result.session_id == "mtqa_test"
    assert result.trace.degraded is False
    assert result.trace.artifact_id == "report-artifact-20260717"
    assert result.trace.confidence == "high"
    assert "降准对银行净息差的长期影响尚不明确" in result.trace.uncertainty
    # source_ids 验证：只保留冻结 sources 中存在的
    assert len(result.trace.sources) == 2
    source_ids = [source.source_id for source in result.trace.sources]
    assert source_ids == ["INDEX_000001_SH", "NEWS_001"]


def test_candidate_source_ids_are_complete_and_follow_snapshot_order() -> None:
    trace = MarketTraceResult.model_validate(VALID_TRACE_DICT)
    candidate = next(item for item in trace.candidates if item.id == "domestic_macro_policy")

    assert _candidate_source_ids(candidate, SNAPSHOT) == [
        "INDEX_000001_SH",
        "NEWS_001",
    ]


def test_qa_prompt_requires_strict_selection_and_complete_ordered_sources() -> None:
    prompt = market_trace_qa_service.MARKET_TRACE_QA_PROMPT
    assert "phenomenon_discovery" in prompt
    assert "phenomenon_kind" in prompt
    assert "完整、有序、逐字照抄" in prompt
    assert "禁止自由事实" in prompt
    assert "实时结论" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_ids",
    [
        ["NEWS_001", "INDEX_000001_SH"],
        ["INDEX_000001_SH"],
        ["INDEX_000001_SH", "NEWS_001", "GLOBAL_001"],
    ],
)
async def test_candidate_requires_exact_complete_ordered_source_ids(
    source_ids: list[str],
) -> None:
    report = {"content": _make_report_content(), "status": "completed"}
    llm_response = _make_llm_response("candidate", source_ids, "domestic_macro_policy")
    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_response)),
        ),
    ):
        result = await answer_market_trace_qa("大盘为何上涨", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.degraded_reason == "模型选择了不完整或乱序的来源"


@pytest.mark.asyncio
async def test_found_report_without_valid_id_keeps_artifact_id_empty() -> None:
    report = {"content": _make_report_content(), "status": "completed"}
    llm_response = _make_llm_response("out_of_scope", [])
    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_response)),
        ),
    ):
        result = await answer_market_trace_qa("范围外问题", _REPORT_DATE, None)

    assert result.trace.degraded is False
    assert result.trace.artifact_id == ""


@pytest.mark.asyncio
async def test_no_phenomenon_returns_fixed_business_answer_without_llm() -> None:
    calm_a_share = json.loads(json.dumps(_A_SHARE))
    indexes = calm_a_share["indexes"]
    for index in indexes.values():
        index["pct_chg"] = 0.1
    calm_a_share["breadth"] = {
        "advance_ratio": 0.5,
        "total_count": 5000,
        "decline_count": 2400,
    }
    calm_a_share["turnover"] = {"change_pct": 1.0}
    calm_a_share["limits"] = {
        "up_count": 10,
        "down_count": 8,
        "broken_count": 1,
        "highest_board": 2,
    }
    discovery = discover_market_phenomenon(calm_a_share, _SOURCES, _CAPTURED_AT, [])
    snapshot = SNAPSHOT.model_copy(
        update={"a_share": calm_a_share, "phenomenon_discovery": discovery}
    )
    empty_trace = {
        "schema_version": "1.1",
        "attribution_status": "not_applicable",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "low",
        "unresolved_questions": ["未检测到明确的市场主导现象"],
    }
    content = _make_report_content()
    market_trace = content["market_trace"]
    assert isinstance(market_trace, dict)
    market_trace["snapshot"] = snapshot.model_dump(mode="json")
    market_trace["trace"] = empty_trace
    report = {"content": content, "status": "completed"}
    llm_factory = MagicMock()
    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch("aistock_agent.services.market_trace_qa.get_deep_think", llm_factory),
    ):
        result = await answer_market_trace_qa("市场发生了什么", _REPORT_DATE, None)

    assert result.trace.degraded is False
    assert result.trace.sources == []
    assert result.content == "行情完整，未发现显著市场现象"
    assert result.trace.uncertainty == ["未检测到明确的市场主导现象"]
    llm_factory.assert_not_called()


@pytest.mark.asyncio
async def test_insufficient_data_returns_fixed_answer_and_readiness_uncertainty() -> None:
    missing_fields = ["a_share.indexes"]
    discovery = discover_market_phenomenon({}, {}, _CAPTURED_AT, missing_fields)
    snapshot = SNAPSHOT.model_copy(
        update={
            "a_share": {},
            "sources": {},
            "missing_fields": missing_fields,
            "phenomenon_discovery": discovery,
        }
    )
    empty_trace = {
        "schema_version": "1.1",
        "attribution_status": "insufficient",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "low",
        "unresolved_questions": [
            "市场数据不足以支撑归因分析",
            "因果证据充分性不足，依赖 partial 或 not_ready 来源",
            "快照缺少 1 个字段",
        ],
    }
    content = _make_report_content()
    market_trace = content["market_trace"]
    assert isinstance(market_trace, dict)
    market_trace["snapshot"] = snapshot.model_dump(mode="json")
    market_trace["trace"] = empty_trace
    report = {"content": content, "status": "completed"}
    llm_factory = MagicMock()
    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch("aistock_agent.services.market_trace_qa.get_deep_think", llm_factory),
    ):
        result = await answer_market_trace_qa("市场发生了什么", _REPORT_DATE, None)

    assert result.trace.degraded is False
    assert result.content == "行情数据不足，无法可靠判断市场现象"
    assert result.trace.uncertainty == empty_trace["unresolved_questions"]
    llm_factory.assert_not_called()


@pytest.mark.asyncio
async def test_no_report_returns_degraded():
    """无当日复盘报告 -> degraded=true，不编造结论。"""
    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=ReviewReportReadResult("not_found")),
    ):
        result = await answer_market_trace_qa("大盘为何涨跌", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.artifact_id == ""
    assert "无市场复盘报告" in (result.trace.degraded_reason or "")
    assert result.content == "暂时无法回答此问题，请稍后重试。"


@pytest.mark.asyncio
async def test_report_without_market_trace_returns_degraded():
    """报告缺少 market_trace 字段 -> degraded。"""
    report = {"content": {"display_report": {"details": "..."}}, "status": "completed"}

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert "market_trace" in (result.trace.degraded_reason or "")


@pytest.mark.asyncio
async def test_date_mismatch_returns_degraded():
    """snapshot.trade_date 与 report_date 不一致 -> degraded。"""
    bad_content = _make_report_content()
    # 篡改 trade_date 使其与 report_date 不匹配
    mt = bad_content["market_trace"]
    if isinstance(mt, dict):
        snap = mt["snapshot"]
        if isinstance(snap, dict):
            snap["trade_date"] = "2026-07-16"

    report = {
        "id": "date-mismatch-artifact",
        "content": bad_content,
        "status": "completed",
    }

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.artifact_id == "date-mismatch-artifact"
    assert "日期不匹配" in (result.trace.degraded_reason or "")


@pytest.mark.asyncio
async def test_source_mapping_mismatch_keeps_found_report_artifact_id() -> None:
    content = _make_report_content()
    market_trace = content["market_trace"]
    assert isinstance(market_trace, dict)
    snapshot = market_trace["snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    assert isinstance(sources, dict)
    news = sources["NEWS_001"]
    assert isinstance(news, dict)
    news["source_id"] = "NEWS_MISMATCH"
    report = {
        "id": "mapping-mismatch-artifact",
        "content": content,
        "status": "completed",
    }

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.artifact_id == "mapping-mismatch-artifact"
    assert "来源映射不一致" in (result.trace.degraded_reason or "")


@pytest.mark.asyncio
async def test_llm_failure_returns_degraded():
    """LLM 调用失败 -> degraded。"""
    report = {"content": _make_report_content(), "status": "completed"}

    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(ainvoke=AsyncMock(side_effect=Exception("LLM timeout"))),
        ),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert "模型调用失败" in (result.trace.degraded_reason or "")


@pytest.mark.asyncio
async def test_invalid_llm_output_returns_degraded():
    """LLM 输出非法 JSON -> degraded。"""
    report = {"content": _make_report_content(), "status": "completed"}

    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(ainvoke=AsyncMock(return_value=AIMessage(content="这不是JSON"))),
        ),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert "格式非法" in (result.trace.degraded_reason or "")


@pytest.mark.asyncio
async def test_llm_output_with_code_fences_parsed():
    """LLM 输出被代码围栏包裹 -> 仍能正确解析。"""
    report = {"content": _make_report_content(), "status": "completed"}
    llm_resp = AIMessage(
        content=(
            "```json\n"
            '{"answer_type":"candidate","candidate_id":"domestic_macro_policy",'
            '"phenomenon_kind":null,'
            '"source_ids":["INDEX_000001_SH","NEWS_001"]}\n```'
        )
    )

    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
        ),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is False
    assert result.content == (
        "复盘候选（已支持）：央行降准释放流动性是主因。"
        "这是已归档复盘中的证据归因，不等同于确认因果关系。"
    )
    assert [source.source_id for source in result.trace.sources] == [
        "INDEX_000001_SH",
        "NEWS_001",
    ]


@pytest.mark.asyncio
async def test_unknown_source_id_returns_degraded():
    """模型选择不存在的来源时必须降级，不能静默过滤。"""
    report = {"content": _make_report_content(), "status": "completed"}
    llm_resp = _make_llm_response(
        "candidate",
        ["NEWS_001", "INVALID_ID", "INDEX_000001_SH"],
        "domestic_macro_policy",
    )

    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
        ),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.as_of == ""


@pytest.mark.asyncio
async def test_default_report_date_uses_shanghai_timezone():
    """未传 report_date 时按 Asia/Shanghai 取当天。"""
    report = {"content": _make_report_content(), "status": "completed"}
    llm_resp = _make_llm_response("out_of_scope", [])

    captured_dates: list[str] = []

    async def mock_get(report_date: date) -> ReviewReportReadResult:
        captured_dates.append(report_date.isoformat())
        return _found_review_report(report)

    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=mock_get,
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
        ),
    ):
        await answer_market_trace_qa("test", None, None)

    # 应该以 Asia/Shanghai 当天日期调用
    assert len(captured_dates) == 1
    assert len(captured_dates[0]) == 10  # YYYY-MM-DD 格式


@pytest.mark.asyncio
async def test_node_api_exception_returns_degraded():
    """报告服务调用异常 -> 明确的暂不可用降级。"""
    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(side_effect=Exception("Connection refused")),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.degraded_reason == "报告服务读取失败/暂不可用"


@pytest.mark.asyncio
async def test_invalid_trace_data_returns_degraded():
    """trace 数据非法（无法通过 model_validate）-> degraded。"""
    bad_content = _make_report_content()
    mt = bad_content["market_trace"]
    if isinstance(mt, dict):
        mt["trace"] = {"schema_version": "invalid"}  # 缺少必填字段

    report = {"content": bad_content, "status": "completed"}

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert "解析失败" in (result.trace.degraded_reason or "")


@pytest.mark.asyncio
async def test_legacy_snapshot_without_discovery_returns_technical_degradation():
    """QA 读取不含 discovery 的持久化工件时必须技术降级。"""
    content = _make_report_content()
    market_trace = content["market_trace"]
    assert isinstance(market_trace, dict)
    snapshot = market_trace["snapshot"]
    assert isinstance(snapshot, dict)
    snapshot.pop("phenomenon_discovery", None)
    report = {
        "id": "legacy-artifact",
        "content": content,
        "status": "completed",
    }

    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(
                ainvoke=AsyncMock(return_value=_make_llm_response("out_of_scope", []))
            ),
        ),
    ):
        response = await answer_market_trace_qa("市场情况如何", _REPORT_DATE, None)

    assert response.trace.degraded is True
    assert response.trace.artifact_id == "legacy-artifact"
    assert response.trace.as_of == ""
    assert response.trace.sources == []
    assert response.trace.degraded_reason == "复盘报告数据解析失败"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        "无效顶层 JSON 字符串",
        None,
        {"answer_type": "out_of_scope", "candidate_id": None, "source_ids": [], "extra": 1},
        {"answer_type": "out_of_scope", "candidate_id": None, "source_ids": [1]},
        {
            "answer_type": "candidate",
            "candidate_id": "domestic_macro_policy",
            "source_ids": ["NEWS_001", "NEWS_001"],
        },
    ],
)
async def test_invalid_llm_selection_returns_degraded_without_as_of(payload: object):
    """顶层、字段类型、重复来源或意外字段均不可被宽松接受。"""
    report = {"content": _make_report_content(), "status": "completed"}

    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(
                ainvoke=AsyncMock(return_value=AIMessage(content=json.dumps(payload)))
            ),
        ),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.as_of == ""


@pytest.mark.asyncio
async def test_source_map_key_must_match_source_record_id():
    """来源 map 键与记录 source_id 不一致时，不得透出伪造 ID。"""
    content = _make_report_content()
    market_trace = content["market_trace"]
    assert isinstance(market_trace, dict)
    snapshot = market_trace["snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    assert isinstance(sources, dict)
    sources["FORGED_KEY"] = sources.pop("NEWS_001")
    report = {"content": content, "status": "completed"}
    llm_resp = _make_llm_response("candidate", ["FORGED_KEY"], "domestic_macro_policy")

    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
        ),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.as_of == ""


@pytest.mark.asyncio
async def test_non_completed_report_returns_degraded_without_as_of():
    """只有 completed 复盘报告才能用于问答。"""
    report = {"content": _make_report_content(), "status": "processing"}

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.as_of == ""


@pytest.mark.asyncio
async def test_no_report_returns_degraded_without_as_of():
    """没有报告时不能把请求日期冒充工件采集时间。"""
    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=ReviewReportReadResult("not_found")),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.as_of == ""


@pytest.mark.asyncio
async def test_llm_free_answer_is_rejected_not_returned():
    """自由文本 answer 不能作为事实回答进入响应。"""
    report = {"content": _make_report_content(), "status": "completed"}
    llm_resp = AIMessage(
        content=json.dumps(
            {
                "answer": "实时行情显示上证指数已上涨 2%，因此政策是唯一原因",
                "source_ids": ["NEWS_001"],
            }
        )
    )

    with (
        patch(
            "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
            new=AsyncMock(return_value=_found_review_report(report)),
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
        ),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert "实时行情" not in result.content


@pytest.mark.asyncio
async def test_invalid_direct_report_date_never_calls_node_or_realtime_reader():
    """绕过 FastAPI 直调服务时，路径穿越日期也必须在 Node 前被拒绝。"""
    legacy_reader = AsyncMock(return_value=None)
    dedicated_reader = AsyncMock()
    realtime_reader = AsyncMock()

    with (
        patch.object(
            market_trace_qa_service.node_api,
            "get_analysis_report",
            new=legacy_reader,
        ),
        patch.object(
            market_trace_qa_service.node_api,
            "get_review_analysis_report",
            new=dedicated_reader,
        ),
        patch.object(
            market_trace_qa_service.node_api,
            "get",
            new=realtime_reader,
        ),
    ):
        result = await answer_market_trace_qa("大盘为何涨跌", "../../quote/600519", None)

    assert result.trace.degraded is True
    assert "报告日期非法" in (result.trace.degraded_reason or "")
    legacy_reader.assert_not_awaited()
    dedicated_reader.assert_not_awaited()
    realtime_reader.assert_not_awaited()


@pytest.mark.asyncio
async def test_tampered_discovery_summary_is_rejected_before_llm():
    """持久化 discovery 被篡改时，校验必须阻断模型调用。"""
    content = _make_report_content()
    market_trace = content["market_trace"]
    assert isinstance(market_trace, dict)
    snapshot = market_trace["snapshot"]
    assert isinstance(snapshot, dict)
    discovery = snapshot["phenomenon_discovery"]
    assert isinstance(discovery, dict)
    primary = discovery["primary"]
    assert isinstance(primary, dict)
    primary["summary"] = "TAMPERED_DISCOVERY_SUMMARY"
    report = {"content": content, "status": "completed"}
    legacy_reader = AsyncMock(return_value=report)
    dedicated_reader = AsyncMock(return_value=ReviewReportReadResult("found", report))
    llm_factory = MagicMock()

    with (
        patch.object(
            market_trace_qa_service.node_api,
            "get_analysis_report",
            new=legacy_reader,
        ),
        patch.object(
            market_trace_qa_service.node_api,
            "get_review_analysis_report",
            new=dedicated_reader,
        ),
        patch(
            "aistock_agent.services.market_trace_qa.get_deep_think",
            llm_factory,
        ),
    ):
        result = await answer_market_trace_qa("主导现象是什么", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert "复盘报告校验失败" in (result.trace.degraded_reason or "")
    legacy_reader.assert_not_awaited()
    dedicated_reader.assert_awaited_once()
    llm_factory.assert_not_called()


@pytest.mark.asyncio
async def test_unavailable_report_service_is_not_reported_as_missing():
    """专用读取返回 unavailable 时，响应必须明确报告服务暂不可用。"""
    legacy_reader = AsyncMock(return_value=None)
    dedicated_reader = AsyncMock(return_value=ReviewReportReadResult("unavailable"))

    with (
        patch.object(
            market_trace_qa_service.node_api,
            "get_analysis_report",
            new=legacy_reader,
        ),
        patch.object(
            market_trace_qa_service.node_api,
            "get_review_analysis_report",
            new=dedicated_reader,
        ),
    ):
        result = await answer_market_trace_qa("大盘为何涨跌", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.degraded_reason == "报告服务读取失败/暂不可用"
    legacy_reader.assert_not_awaited()
    dedicated_reader.assert_awaited_once()


def test_render_phenomenon_discovery_uses_snapshot_summary() -> None:
    """现象选择只能渲染冻结 snapshot.discovery。"""
    trace = MarketTraceResult.model_validate(VALID_TRACE_DICT)
    primary = SNAPSHOT.phenomenon_discovery.primary
    assert primary is not None
    selection = _MarketTraceQaSelection(
        answer_type="phenomenon_discovery",
        candidate_id=None,
        phenomenon_kind=primary.kind,
        source_ids=primary.fact_ids,
    )

    content, source_ids = _render_selection(selection, SNAPSHOT, trace)

    assert primary.summary in content
    assert source_ids == primary.fact_ids
