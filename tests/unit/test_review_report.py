"""review 报告渲染单元测试

覆盖：
- _build_review_report（保留：schema v2 持久化用，Task 4 不动）
- render_market_trace_markdown（新增：brief Step 4 的展示层 Markdown 模板）
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from aistock_agent.agents.workers import review
from aistock_agent.schemas.market_trace import (
    DataReadiness,
    DetectedPhenomenon,
    EventHit,
    MarketTraceResult,
    MarketTraceSnapshot,
    PhenomenonDiscoveryResult,
    PredictionValidation,
    ReviewArtifact,
    SectorHit,
    SourceRecord,
)

# ============================================================================
# 保留：_build_review_report 的测试（持久化 schema v2）
# ============================================================================

# 使用 render_market_trace_markdown 产出的新 markdown 格式（## 主导现象 段），
# 确保 _build_review_report 能从新格式提取非空 summary。
REVIEW_MARKDOWN = """# A股收盘溯源｜2026-07-17
快照编号：trace-20260717

## 确认的市场现象
- 类型：broad_rally
- 摘要：市场风险偏好改善，科技板块领涨。
- 评分：3

## 归因结论
- 未选定主因。

<!--SECTOR_LIST_START-->
- 半导体
- AI算力
<!--SECTOR_LIST_END-->
"""


def test_build_review_report_uses_schema_v2_and_keeps_markdown():
    artifact = ReviewArtifact(
        schema_version="1.1",
        snapshot=_RENDER_SNAPSHOT,
        trace=_make_render_trace(),
        markdown=REVIEW_MARKDOWN,
        trace_summary="市场风险偏好改善，科技板块领涨。",
        sectors=["半导体", "AI算力"],
    )
    report = review._build_review_report(artifact)
    assert report["display_report"]["summary"] == "市场风险偏好改善，科技板块领涨。"
    assert report["display_report"]["details"] == REVIEW_MARKDOWN
    assert report["display_report"]["stocks"] == []
    assert report["display_report"]["sectors"] == ["半导体", "AI算力"]
    assert report["display_report"]["risks"] == ["降准对银行净息差的长期影响尚不明确"]
    assert report["podcast_brief"] == ""
    assert report["schema_version"] == "2.0"
    assert report["snapshot_id"] == "trace-20260717"
    assert "snapshot" in report["market_trace"]
    assert "trace" in report["market_trace"]


def test_extract_review_sectors_falls_back_to_appendix_b_fixture():
    """_extract_review_sectors 无 SECTOR_LIST 标记时回退到附录B 表格第一列。"""
    markdown = Path("tests/fixtures/sample_review_report.md").read_text(encoding="utf-8")
    sectors = review._extract_review_sectors(markdown)
    assert sectors == ["黄金", "贵金属", "半导体", "新能源车"]


# ============================================================================
# 新增：render_market_trace_markdown 的测试（brief Step 4 展示层模板）
# ============================================================================

_CAPTURED_AT = datetime(2026, 7, 17, 15, 30, tzinfo=UTC)
_TRADE_DATE = datetime(2026, 7, 17, 15, 0, tzinfo=UTC)


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


_RENDER_SNAPSHOT = MarketTraceSnapshot(
    snapshot_id="trace-20260717",
    trade_date="2026-07-17",
    captured_at=_CAPTURED_AT,
    a_share={
        "sectors": {
            "top_gainers": [{"name": "半导体"}],
            "top_losers": [{"name": "房地产"}],
            "top_inflows": [],
            "top_outflows": [],
        },
    },
    sources={
        "INDEX_000001_SH": _make_source(
            "INDEX_000001_SH",
            provider="tushare:index_daily",
            title="上证指数",
            content="close=3200.0, pct_chg=0.5",
        ),
        "GLOBAL_001": _make_source(
            "GLOBAL_001",
            provider="yfinance",
            title="标普500",
            content="price=5500.0, change_pct=0.36",
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
    phenomenon_discovery=PhenomenonDiscoveryResult(
        status="detected",
        primary=DetectedPhenomenon(
            kind="broad_rally",
            summary="多个核心指数同步上涨，市场广度偏强",
            fact_ids=["INDEX_000001_SH"],
            tags=["broad_rally"],
            severity="high",
        ),
        concurrent_phenomena=[],
        data_readiness=DataReadiness(
            market_data="complete",
            attribution_inputs="complete",
            causal_evidence="ready",
        ),
        diagnostics=[],
    ),
)


_RENDER_TRACE_DICT: dict[str, object] = {
    "schema_version": "1.1",
    "attribution_status": "confirmed",
    "candidates": [
        {
            "id": "global_risk_liquidity",
            "category": "global_risk_liquidity",
            "status": "weak",
            "verdict": "全球风险偏好改善但非主因",
            "chain": {
                "nodes": [
                    {
                        "stage": "structural_root",
                        "claim": "美联储维持利率",
                        "evidence_ids": ["SEARCH_001"],
                    },
                    {
                        "stage": "trigger",
                        "claim": "全球流动性宽松预期",
                        "evidence_ids": ["GLOBAL_001"],
                    },
                    {
                        "stage": "transmission",
                        "claim": "外资流入新兴市场",
                        "evidence_ids": ["GLOBAL_001"],
                    },
                    {
                        "stage": "exposure",
                        "claim": "北向资金净流入",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                    {
                        "stage": "repricing",
                        "claim": "权重股估值抬升",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                    {
                        "stage": "observable_result",
                        "claim": "上证指数上涨0.5%",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                ]
            },
            "supporting_evidence_ids": ["GLOBAL_001", "SEARCH_001"],
            "counter_evidence_ids": [],
        },
        {
            "id": "domestic_macro_policy",
            "category": "domestic_macro_policy",
            "status": "supported",
            "verdict": "央行降准释放流动性是主因",
            "chain": {
                "nodes": [
                    {
                        "stage": "structural_root",
                        "claim": "国内货币政策宽松周期",
                        "evidence_ids": ["NEWS_001"],
                    },
                    {
                        "stage": "trigger",
                        "claim": "央行宣布降准0.5个百分点",
                        "evidence_ids": ["NEWS_001"],
                    },
                    {
                        "stage": "transmission",
                        "claim": "银行间流动性宽松传导至权益",
                        "evidence_ids": ["NEWS_001"],
                    },
                    {
                        "stage": "exposure",
                        "claim": "金融板块直接受益",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                    {
                        "stage": "repricing",
                        "claim": "市场情绪回暖",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                    {
                        "stage": "observable_result",
                        "claim": "上证指数上涨0.5%",
                        "evidence_ids": ["INDEX_000001_SH"],
                    },
                ]
            },
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


def _make_render_trace() -> MarketTraceResult:
    return MarketTraceResult.model_validate_json(json.dumps(_RENDER_TRACE_DICT, ensure_ascii=False))


def test_render_market_trace_markdown_includes_required_sections():
    """渲染的 Markdown 包含 brief Step 4 要求的所有固定章节。"""
    trace = _make_render_trace()
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "# A股收盘溯源｜2026-07-17" in markdown
    assert "快照编号：trace-20260717" in markdown
    assert "## 确认的市场现象" in markdown
    assert "## 归因结论" in markdown
    assert "## 候选解释与反证" in markdown
    assert "## 缺失证据" in markdown
    assert "## 证据索引" in markdown
    assert "## 未解问题" in markdown


def test_render_market_trace_markdown_includes_sector_list_marker():
    """渲染的 Markdown 末尾包含 SECTOR_LIST 标记和板块名。"""
    trace = _make_render_trace()
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "<!--SECTOR_LIST_START-->" in markdown
    assert "<!--SECTOR_LIST_END-->" in markdown
    assert "- 半导体" in markdown
    assert "- 房地产" in markdown


def test_render_market_trace_markdown_shows_evidence_details():
    """证据索引显示 source_id、提供方、时间和 URL。"""
    trace = _make_render_trace()
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "NEWS_001" in markdown
    assert "cls" in markdown
    assert "https://www.cls.cn/news/1" in markdown
    assert "SEARCH_001" in markdown
    assert "tavily" in markdown


def test_render_market_trace_markdown_renders_primary_chain_stages():
    """主因果链渲染 6 个阶段名。"""
    trace = _make_render_trace()
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "structural_root" in markdown
    assert "trigger" in markdown
    assert "transmission" in markdown
    assert "exposure" in markdown
    assert "repricing" in markdown
    assert "observable_result" in markdown


def test_render_market_trace_markdown_renders_alternative_explanation():
    """备选解释渲染 alternative 候选。"""
    trace = _make_render_trace()
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "global_risk_liquidity" in markdown
    assert "全球风险偏好改善但非主因" in markdown


def test_render_market_trace_markdown_renders_rejected_candidates():
    """已排除或证据不足的解释渲染 rejected / insufficient 候选。"""
    trace = _make_render_trace()
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "market_positioning_liquidity" in markdown
    assert "industry_technology_supply" in markdown


def test_render_market_trace_markdown_renders_unresolved_questions():
    """未解问题渲染。"""
    trace = _make_render_trace()
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "降准对银行净息差的长期影响尚不明确" in markdown


def test_render_market_trace_markdown_renders_dominant_phenomenon():
    """主导现象渲染。"""
    trace = _make_render_trace()
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "broad_rally" in markdown
    assert "多个核心指数同步上涨，市场广度偏强" in markdown


def test_render_market_trace_markdown_handles_no_phenomenon():
    """no_phenomenon 使用服务端固定业务文本。"""
    discovery = PhenomenonDiscoveryResult(
        status="no_phenomenon",
        primary=None,
        concurrent_phenomena=[],
        data_readiness=_RENDER_SNAPSHOT.phenomenon_discovery.data_readiness,
        diagnostics=[],
    )
    snapshot = _RENDER_SNAPSHOT.model_copy(update={"phenomenon_discovery": discovery})
    trace = MarketTraceResult(
        schema_version="1.1",
        attribution_status="not_applicable",
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="low",
        unresolved_questions=["未检测到明确的市场主导现象"],
    )
    markdown = review.render_market_trace_markdown(trace, snapshot)

    assert "## 确认的市场现象" in markdown
    assert "行情完整，未发现显著市场现象" in markdown
    assert "未检测到明确的市场主导现象" in markdown
    assert "不适用因果归因" in markdown


def test_render_market_trace_markdown_uses_snapshot_discovery_text():
    """现象文本只来自冻结 snapshot.discovery。"""
    trace = _make_render_trace()
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "多个核心指数同步上涨，市场广度偏强" in markdown
    assert "dominant_phenomenon" not in MarketTraceResult.model_fields


# ============================================================================
# 新增：预判对照章节渲染测试（Task 9）
# ============================================================================


def test_render_markdown_with_prediction_validation():
    """含 prediction_validation 时渲染预判对照章节。"""
    pv = PredictionValidation(
        status="partial",
        sector_hits=[
            SectorHit(
                sector="券商",
                morning_direction="bullish",
                actual_direction="bearish",
                result="miss",
                deviation_note="政策利好未兑现",
            ),
        ],
        event_hits=[
            EventHit(
                event_title="央行降准",
                morning_direction="bullish",
                actual_impact="利好兑现",
                result="hit",
            ),
        ],
        overall_note="板块方向部分偏离",
    )
    trace = _make_render_trace().model_copy(update={"prediction_validation": pv})
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "## 预判对照" in markdown
    assert "部分命中" in markdown
    assert "券商" in markdown
    assert "偏离" in markdown
    assert "政策利好未兑现" in markdown
    assert "央行降准" in markdown
    assert "板块方向部分偏离" in markdown


def test_render_markdown_prediction_validation_none_degraded():
    """prediction_validation=None 时渲染降级文案。"""
    trace = _make_render_trace()
    # _make_render_trace 默认 prediction_validation=None
    assert trace.prediction_validation is None
    markdown = review.render_market_trace_markdown(trace, _RENDER_SNAPSHOT)

    assert "## 预判对照" in markdown
    assert "无晨报预测可对照" in markdown


def test_extract_trace_summary_prefers_summary_line_over_type_line():
    """摘要应提取"- 摘要：xxx"行（易读中文），而非"- 类型：broad_rally"内部字段行。"""
    summary = review._extract_trace_summary(REVIEW_MARKDOWN)
    assert summary == "市场风险偏好改善，科技板块领涨。"


def test_extract_trace_summary_falls_back_to_first_line_without_summary_line():
    """现象段缺失摘要行时，回退到段落首个有效行（兼容旧 markdown）。"""
    markdown = """# A股收盘溯源｜2026-07-17

## 确认的市场现象
- 类型：sector_rotation
- 严重度：medium

## 归因结论
- 证据不足，未确认主因。
"""
    summary = review._extract_trace_summary(markdown)
    assert summary == "类型：sector_rotation"


def test_extract_trace_summary_falls_back_to_step_four_for_legacy_markdown():
    """旧 markdown 无现象段时，回退到 ## 步骤4 段首个有效行。"""
    legacy = """# A股收盘溯源｜2026-07-17

## 步骤4 输出核心结论
今日市场震荡上行，结构性机会明显。

## 归因结论
- 未选定主因。
"""
    summary = review._extract_trace_summary(legacy)
    assert summary == "今日市场震荡上行，结构性机会明显。"
