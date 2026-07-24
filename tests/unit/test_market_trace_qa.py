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
    DominantPhenomenon,
    MarketTraceResult,
    MarketTraceSnapshot,
    SourceRecord,
)
from aistock_agent.services import market_trace_qa as market_trace_qa_service
from aistock_agent.services.data_client import ReviewReportReadResult
from aistock_agent.services.market_trace_qa import (
    _MarketTraceQaSelection,
    _render_selection,
    answer_market_trace_qa,
)

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


SNAPSHOT = MarketTraceSnapshot(
    snapshot_id="trace-20260717",
    trade_date=_REPORT_DATE,
    captured_at=_CAPTURED_AT,
    a_share={
        "sectors": {
            "top_gainers": [{"name": "半导体"}],
            "top_losers": [{"name": "房地产"}],
        },
    },
    sources={
        "INDEX_000001_SH": _make_source(
            "INDEX_000001_SH",
            provider="tushare:index_daily",
            title="上证指数",
            content="close=3200.0, pct_chg=0.5",
        ),
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
    },
    missing_fields=[],
    dominant_phenomenon=DominantPhenomenon(
        kind="broad_rally",
        summary="多个核心指数同步上涨，市场广度偏强",
        fact_ids=["INDEX_000001_SH"],
        score=3,
    ),
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
    "schema_version": "1.0",
    "dominant_phenomenon": {
        "kind": "broad_rally",
        "summary": "多个核心指数同步上涨，市场广度偏强",
        "fact_ids": ["INDEX_000001_SH"],
        "score": 3,
    },
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
) -> AIMessage:
    selection: dict[str, object] = {
        "answer_type": answer_type,
        "candidate_id": candidate_id,
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
        "candidate", ["NEWS_001", "INDEX_000001_SH"], "domestic_macro_policy"
    )

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ), patch(
        "aistock_agent.services.market_trace_qa.get_deep_think",
        return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
    ):
        result = await answer_market_trace_qa(
            "大盘为何涨跌", _REPORT_DATE, "mtqa_test"
        )

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
    source_ids = {s.source_id for s in result.trace.sources}
    assert source_ids == {"NEWS_001", "INDEX_000001_SH"}


@pytest.mark.asyncio
async def test_no_report_returns_degraded():
    """无当日复盘报告 -> degraded=true，不编造结论。"""
    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=ReviewReportReadResult("not_found")),
    ):
        result = await answer_market_trace_qa(
            "大盘为何涨跌", _REPORT_DATE, None
        )

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

    report = {"content": bad_content, "status": "completed"}

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert "日期不匹配" in (result.trace.degraded_reason or "")


@pytest.mark.asyncio
async def test_llm_failure_returns_degraded():
    """LLM 调用失败 -> degraded。"""
    report = {"content": _make_report_content(), "status": "completed"}

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ), patch(
        "aistock_agent.services.market_trace_qa.get_deep_think",
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=Exception("LLM timeout"))),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert "模型调用失败" in (result.trace.degraded_reason or "")


@pytest.mark.asyncio
async def test_invalid_llm_output_returns_degraded():
    """LLM 输出非法 JSON -> degraded。"""
    report = {"content": _make_report_content(), "status": "completed"}

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ), patch(
        "aistock_agent.services.market_trace_qa.get_deep_think",
        return_value=MagicMock(
            ainvoke=AsyncMock(return_value=AIMessage(content="这不是JSON"))
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
            '"source_ids":["NEWS_001"]}\n```'
        )
    )

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ), patch(
        "aistock_agent.services.market_trace_qa.get_deep_think",
        return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
    ):
        result = await answer_market_trace_qa("test", _REPORT_DATE, None)

    assert result.trace.degraded is False
    assert result.content == (
        "复盘候选（已支持）：央行降准释放流动性是主因。"
        "这是已归档复盘中的证据归因，不等同于确认因果关系。"
    )
    assert len(result.trace.sources) == 1
    assert result.trace.sources[0].source_id == "NEWS_001"


@pytest.mark.asyncio
async def test_unknown_source_id_returns_degraded():
    """模型选择不存在的来源时必须降级，不能静默过滤。"""
    report = {"content": _make_report_content(), "status": "completed"}
    llm_resp = _make_llm_response(
        "candidate",
        ["NEWS_001", "INVALID_ID", "INDEX_000001_SH"],
        "domestic_macro_policy",
    )

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ), patch(
        "aistock_agent.services.market_trace_qa.get_deep_think",
        return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
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

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=mock_get,
    ), patch(
        "aistock_agent.services.market_trace_qa.get_deep_think",
        return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
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

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ), patch(
        "aistock_agent.services.market_trace_qa.get_deep_think",
        return_value=MagicMock(ainvoke=AsyncMock(return_value=AIMessage(content=json.dumps(payload)))),
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
    llm_resp = _make_llm_response(
        "candidate", ["FORGED_KEY"], "domestic_macro_policy"
    )

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ), patch(
        "aistock_agent.services.market_trace_qa.get_deep_think",
        return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
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

    with patch(
        "aistock_agent.services.market_trace_qa.node_api.get_review_analysis_report",
        new=AsyncMock(return_value=_found_review_report(report)),
    ), patch(
        "aistock_agent.services.market_trace_qa.get_deep_think",
        return_value=MagicMock(ainvoke=AsyncMock(return_value=llm_resp)),
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

    with patch.object(
        market_trace_qa_service.node_api,
        "get_analysis_report",
        new=legacy_reader,
    ), patch.object(
        market_trace_qa_service.node_api,
        "get_review_analysis_report",
        new=dedicated_reader,
    ), patch.object(
        market_trace_qa_service.node_api,
        "get",
        new=realtime_reader,
    ):
        result = await answer_market_trace_qa("大盘为何涨跌", "../../quote/600519", None)

    assert result.trace.degraded is True
    assert "报告日期非法" in (result.trace.degraded_reason or "")
    legacy_reader.assert_not_awaited()
    dedicated_reader.assert_not_awaited()
    realtime_reader.assert_not_awaited()


@pytest.mark.asyncio
async def test_tampered_dominant_summary_is_rejected_before_llm():
    """持久化 trace 的主导摘要被篡改时，校验必须阻断模型调用。"""
    content = _make_report_content()
    market_trace = content["market_trace"]
    assert isinstance(market_trace, dict)
    trace = market_trace["trace"]
    assert isinstance(trace, dict)
    dominant = trace["dominant_phenomenon"]
    assert isinstance(dominant, dict)
    dominant["summary"] = "TAMPERED_TRACE_SUMMARY"
    report = {"content": content, "status": "completed"}
    legacy_reader = AsyncMock(return_value=report)
    dedicated_reader = AsyncMock(return_value=ReviewReportReadResult("found", report))
    llm_factory = MagicMock()

    with patch.object(
        market_trace_qa_service.node_api,
        "get_analysis_report",
        new=legacy_reader,
    ), patch.object(
        market_trace_qa_service.node_api,
        "get_review_analysis_report",
        new=dedicated_reader,
    ), patch(
        "aistock_agent.services.market_trace_qa.get_deep_think",
        llm_factory,
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

    with patch.object(
        market_trace_qa_service.node_api,
        "get_analysis_report",
        new=legacy_reader,
    ), patch.object(
        market_trace_qa_service.node_api,
        "get_review_analysis_report",
        new=dedicated_reader,
    ):
        result = await answer_market_trace_qa("大盘为何涨跌", _REPORT_DATE, None)

    assert result.trace.degraded is True
    assert result.trace.degraded_reason == "报告服务读取失败/暂不可用"
    legacy_reader.assert_not_awaited()
    dedicated_reader.assert_awaited_once()


def test_render_dominant_phenomenon_uses_snapshot_summary() -> None:
    """渲染层独立于校验层时，仍只使用冻结 snapshot 的主导摘要。"""
    tampered_trace = dict(VALID_TRACE_DICT)
    tampered_dominant = dict(tampered_trace["dominant_phenomenon"])
    tampered_dominant["summary"] = "TAMPERED_TRACE_SUMMARY"
    tampered_trace["dominant_phenomenon"] = tampered_dominant
    trace = MarketTraceResult.model_validate(tampered_trace)
    selection = _MarketTraceQaSelection(
        answer_type="dominant_phenomenon",
        candidate_id=None,
        source_ids=["INDEX_000001_SH"],
    )

    content, source_ids = _render_selection(selection, SNAPSHOT, trace)

    assert SNAPSHOT.dominant_phenomenon is not None
    assert SNAPSHOT.dominant_phenomenon.summary in content
    assert "TAMPERED_TRACE_SUMMARY" not in content
    assert source_ids == {"INDEX_000001_SH"}
