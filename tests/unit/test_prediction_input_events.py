"""Spec §4.2 / 计划 Task 3.1：预判依据增强（链上事件 + 中台事件 + 大盘归因注入 + 留痕）。

覆盖：
- 大盘路径（run_predict）：链上事件（全部 children，≤5）/ 中台匹配（≤3）/ 大盘归因摘要；
  无链 + 中台无匹配 → 三个键都不注入（不注入空数组/占位），不报错；
- 板块路径（predict_sector）：该板块在链上 → 注入其 children[].events（≤5）；
  不在链上 → 省略 chain_events（保留 market_trace_brief）；
- 留痕：PredictionResult.input_event_refs 与注入集合一致；无注入 → 空数组（非 None）；
- 回放隔离：replay_context 存在时不读链（P4 回放零 DB/网络访问）。
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.schemas.market_trace import (
    CandidateExplanation,
    MarketTraceResult,
    MarketTraceSnapshot,
)
from aistock_agent.schemas.prediction import PredictionResult
from aistock_agent.services import prediction_service as ps

_REPORT_DATE = "2026-08-10"

_VALID_LLM_JSON = """{
  "schema_version": "3.0",
  "prediction_status": "confirmed",
  "horizons": [
    {"horizon": "short", "remaining_estimate": "1-3 日", "phase": "decaying",
     "direction": "bearish", "target": "上证指数", "metric_projection": "短期弱震荡",
     "confidence": "high"},
    {"horizon": "mid", "remaining_estimate": "2-4 周", "phase": "peaking",
     "direction": "bearish", "target": "上证指数", "metric_projection": "指数区间下移",
     "confidence": "medium"}
  ],
  "evolution_narrative": "短线已兑现大半，中线延续",
  "risks": [{"factor": "政策对冲", "invalidation": "超预期政策落地则失效"}],
  "evidence_ids": ["m1"],
  "attribution_summary": "利空影响短线衰减、中线延续"
}"""


def _make_snapshot(trade_date: str = _REPORT_DATE) -> MarketTraceSnapshot:
    return MarketTraceSnapshot(
        snapshot_id="snap-1",
        trade_date=trade_date,
        captured_at=datetime(2026, 8, 10, tzinfo=UTC),
        a_share={"indices": [], "sectors": {}},
        sources={},
        missing_fields=[],
        phenomenon_discovery={
            "status": "detected",
            "primary": {
                "kind": "broad_decline",
                "summary": "大盘普跌",
                "fact_ids": ["m1"],
                "tags": [],
                "severity": "high",
            },
            "concurrent_phenomena": [],
            "data_readiness": {
                "market_data": "complete",
                "attribution_inputs": "complete",
                "causal_evidence": "ready",
            },
            "diagnostics": [],
        },
    )


def _make_trace(
    attribution_status: str = "confirmed", attribution_summary: str | None = None
) -> MarketTraceResult:
    """最小大盘溯源结果（主因候选资金面 → sector_rotation 白名单 short+mid）。"""
    has_primary = attribution_status in {"confirmed", "hypothesis"}
    return MarketTraceResult(
        schema_version="1.1",
        attribution_status=attribution_status,
        attribution_summary=attribution_summary,
        candidates=[
            CandidateExplanation(
                id="market_positioning_liquidity",
                category="market_positioning_liquidity",
                status="supported",
                verdict="资金与情绪主导短线走势，无中期基本面依据",
                chain=None,
                supporting_evidence_ids=["m1"],
                counter_evidence_ids=[],
            )
        ] if has_primary else [],
        primary_chain_id="market_positioning_liquidity" if has_primary else None,
        alternative_chain_id=None,
        confidence="high" if attribution_status == "confirmed" else "low",
        unresolved_questions=[],
    )


def _event(event_id: str | None, ref: str, headline: str, source: str = "warehouse") -> dict:
    return {"event_id": event_id, "ref": ref, "headline": headline, "source": source}


# 当日链：半导体 4 条事件 + 煤炭 2 条（大盘路径取全部 → 上限 5 截断）
_CHAIN: dict[str, object] = {
    "date": _REPORT_DATE,
    "root": {
        "type": "market",
        "date": _REPORT_DATE,
        "summary": "资源与半导体共驱大盘",
        "index_pct": 0.8,
    },
    "children": [
        {
            "sector": "半导体",
            "relation": "self_driven",
            "pct": 3.2,
            "trace_summary": "出口管制扰动",
            "events": [
                _event("evt_1", "https://x/1", "半导体出口管制升级"),
                _event("evt_2", "https://x/2", "存储芯片涨价"),
                _event(None, "https://x/3", "设备订单回暖", "search"),
                _event("evt_4", "https://x/4", "晶圆厂扩产"),
            ],
        },
        {
            "sector": "煤炭",
            "relation": "market_follow",
            "pct": 1.1,
            "trace_summary": "跟随大盘",
            "events": [
                _event("evt_5", "https://x/5", "动力煤价格回升"),
                _event("evt_6", "https://x/6", "进口煤配额收紧"),
            ],
        },
    ],
}

# 中台存量事件：3 条命中（半导体/煤炭 权重 3，另 1 条标题命中权重 2）+ 1 条 summary 命中 + 1 条无关
_WAREHOUSE_EVENTS: list[dict[str, object]] = [
    {
        "event_id": "wh_1", "title": "半导体设备招标放量", "url": "https://w/1",
        "impact_score": 5, "involved_keywords": ["半导体"], "industry": "", "summary": "",
    },
    {
        "event_id": "wh_2", "title": "煤炭进口关税调整", "url": "https://w/2",
        "impact_score": 3, "involved_keywords": ["煤炭"], "industry": "", "summary": "",
    },
    {
        "event_id": "wh_4", "title": "半导体材料涨价", "url": "https://w/4",
        "impact_score": 2, "involved_keywords": [], "industry": "半导体", "summary": "",
    },
    {
        "event_id": "wh_3", "title": "市场情绪综述", "url": "https://w/3",
        "impact_score": 9, "involved_keywords": [], "industry": "", "summary": "半导体领涨",
    },
    {
        "event_id": "wh_9", "title": "白酒动销回暖", "url": "https://w/9",
        "impact_score": 5, "involved_keywords": ["白酒"], "industry": "食品饮料", "summary": "",
    },
]

# 大盘路径期望：链上前 5 条（半导体 4 + 煤炭首条）+ 中台权重前 3（wh_1/wh_2/wh_4）
_MARKET_EXPECTED_REFS = ["evt_1", "evt_2", "https://x/3", "evt_4", "evt_5", "wh_1", "wh_2", "wh_4"]


def _capture_llm(captured: dict[str, str]) -> AsyncMock:
    """捕获 run_predict 的 HumanMessage 输入并返回合法 LLM JSON。"""

    async def _ainvoke(messages: list[object]) -> AsyncMock:
        for message in messages:
            if isinstance(message, HumanMessage):
                captured["human"] = str(message.content)
        return AsyncMock(content=_VALID_LLM_JSON)

    llm = AsyncMock()
    llm.ainvoke = AsyncMock(side_effect=_ainvoke)
    return llm


async def _run_predict_capturing(
    *,
    chain: dict[str, object] | None,
    warehouse: list[dict[str, object]] | None = None,
    replay_context: dict[str, object] | None = None,
    trace: MarketTraceResult | None = None,
) -> tuple[ps.PredictionRunResult, dict[str, object], AsyncMock]:
    captured: dict[str, str] = {}
    chain_mock = AsyncMock(return_value=chain)
    warehouse_mock = AsyncMock(return_value=list(warehouse or []))
    with (
        patch.object(ps.node_api, "get_attribution_chain", chain_mock),
        patch(
            "aistock_agent.services.event_store.load_event_scrape", warehouse_mock
        ),
        patch.object(ps, "get_deep_think", return_value=_capture_llm(captured)),
    ):
        result = await ps.run_predict(
            trace if trace is not None else _make_trace(),
            _make_snapshot(),
            replay_context=replay_context,
        )
    return result, json.loads(captured["human"]), chain_mock


# ---------- 大盘路径（run_predict） ----------


@pytest.mark.asyncio
async def test_run_predict_injects_chain_warehouse_and_summary_with_refs() -> None:
    """有链：链上事件 ≤5（全 children）+ 中台 ≤3 + 链根摘要；留痕与注入集合一致。"""
    result, prompt_input, _ = await _run_predict_capturing(
        chain=_CHAIN, warehouse=_WAREHOUSE_EVENTS
    )
    assert result.status == "ok"
    assert result.prediction is not None
    chain_events = prompt_input["chain_events"]
    assert len(chain_events) == 5
    assert [e["headline"] for e in chain_events][0] == "半导体出口管制升级"
    # 每条事件至少含 event_id 或 ref（留痕可追溯）
    assert all(e.get("event_id") or e.get("ref") for e in chain_events)
    warehouse_events = prompt_input["warehouse_events"]
    assert len(warehouse_events) == 3
    assert [e["event_id"] for e in warehouse_events] == ["wh_1", "wh_2", "wh_4"]
    assert prompt_input["attribution_summary"] == "资源与半导体共驱大盘"
    assert result.prediction.input_event_refs == _MARKET_EXPECTED_REFS


@pytest.mark.asyncio
async def test_run_predict_without_chain_and_warehouse_match_omits_all_keys() -> None:
    """无链且中台无匹配：三个键都不注入（不注入空数组/占位），且不报错。"""
    result, prompt_input, chain_mock = await _run_predict_capturing(chain=None)
    assert result.status == "ok"
    assert result.prediction is not None
    chain_mock.assert_awaited_once_with(_REPORT_DATE)
    assert "chain_events" not in prompt_input
    assert "warehouse_events" not in prompt_input
    assert "attribution_summary" not in prompt_input
    assert result.prediction.input_event_refs == []  # 空数组，不是 None


@pytest.mark.asyncio
async def test_run_predict_summary_falls_back_to_trace_conclusion_without_chain() -> None:
    """无链 → attribution_summary 回退溯源结论（同一来源），事件键仍不注入空数组。"""
    result, prompt_input, _ = await _run_predict_capturing(
        chain=None,
        warehouse=_WAREHOUSE_EVENTS,
        trace=_make_trace(attribution_summary="利空短线衰减、中线延续"),
    )
    assert result.status == "ok"
    assert result.prediction is not None
    assert "chain_events" not in prompt_input
    assert "warehouse_events" not in prompt_input
    assert prompt_input["attribution_summary"] == "利空短线衰减、中线延续"
    assert result.prediction.input_event_refs == []


@pytest.mark.asyncio
async def test_run_predict_replay_does_not_read_chain() -> None:
    """P4 回放态：零链读取（回放隔离），留痕恒空数组。"""
    result, prompt_input, chain_mock = await _run_predict_capturing(
        chain=_CHAIN, warehouse=_WAREHOUSE_EVENTS, replay_context={"replay": True}
    )
    assert result.status == "ok"
    assert result.prediction is not None
    chain_mock.assert_not_awaited()
    assert "chain_events" not in prompt_input
    assert result.prediction.input_event_refs == []


# ---------- 板块路径（predict_sector） ----------

_RESOLVED: dict[str, str] = {"ts_code": "BK1001", "name": "存储板块"}

# 板块链路：存储板块 6 条事件（截断 5）+ 煤炭 1 条（不属该板块）
_SECTOR_CHAIN: dict[str, object] = {
    "date": "2026-07-16",
    "root": {"type": "market", "date": "2026-07-16", "summary": "存储领涨", "index_pct": 1.0},
    "children": [
        {
            "sector": "存储板块",
            "relation": "self_driven",
            "pct": 4.0,
            "trace_summary": "存储涨价",
            "events": [
                _event("evt_s1", "https://s/1", "存储芯片报价上调"),
                _event("evt_s2", "https://s/2", "大厂减产"),
                _event(None, "https://s/3", "设备订单回暖", "search"),
                _event("evt_s4", "https://s/4", "HBM 需求爆发"),
                _event("evt_s5", "https://s/5", "渠道库存低位"),
                _event("evt_s6", "https://s/6", "扩产计划落地"),
            ],
        },
        {
            "sector": "煤炭",
            "relation": "market_follow",
            "pct": 0.5,
            "trace_summary": "跟随大盘",
            "events": [_event("evt_c1", "https://c/1", "动力煤价格回升")],
        },
    ],
}

_SECTOR_WAREHOUSE: list[dict[str, object]] = [
    {
        "event_id": "wh_s1", "title": "存储芯片报价上调", "url": "https://w/s1",
        "impact_score": 4, "involved_keywords": ["存储"], "industry": "", "summary": "",
    },
    {
        "event_id": "wh_s2", "title": "白酒动销回暖", "url": "https://w/s2",
        "impact_score": 9, "involved_keywords": ["白酒"], "industry": "", "summary": "",
    },
]


def _sector_prediction() -> PredictionResult:
    return PredictionResult(
        schema_version="3.0",
        prediction_status="hypothesis",
        horizons=[{
            "horizon": "short",
            "remaining_estimate": "1-3 日",
            "phase": "peaking",
            "direction": "bullish",
            "target": "存储板块",
            "metric_projection": "相对现价区间波动",
            "confidence": "medium",
        }],
        evolution_narrative="存储涨价传导，板块短线强势",
        risks=[],
        evidence_ids=[],
    )


async def _run_predict_sector_capturing(
    *, chain: dict[str, object] | None, warehouse: list[dict[str, object]] | None = None
) -> tuple[PredictionResult | None, dict[str, object], dict[str, object]]:
    captured: dict[str, object] = {}

    async def _ainvoke(messages: list[object]) -> PredictionResult:
        for message in messages:
            if isinstance(message, HumanMessage):
                captured["human"] = str(message.content)
        return _sector_prediction()

    structured = MagicMock(ainvoke=AsyncMock(side_effect=_ainvoke))
    save_mock = AsyncMock(return_value={"id": "p1"})
    with (
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="大盘结论一句话")),
        patch.object(ps.node_api, "list_predictions", AsyncMock(return_value=[])),
        patch.object(ps.node_api, "get_attribution_chain", AsyncMock(return_value=chain)),
        patch.object(ps.node_api, "save_prediction", save_mock),
        patch(
            "aistock_agent.services.event_store.load_event_scrape",
            AsyncMock(return_value=list(warehouse or [])),
        ),
        patch.object(ps, "get_quick_think", return_value=MagicMock()),
        patch.object(ps, "with_chat_structured_output", return_value=structured),
    ):
        out = await ps.predict_sector(
            report_date="2026-07-16",
            sector_name="存储板块",
            sector_snapshot={"sector": {"name": "存储板块"}},
        )
    captured["payload"] = json.dumps(save_mock.await_args.args[0], ensure_ascii=False)
    return (
        out,
        json.loads(str(captured["human"])),
        json.loads(str(captured["payload"])),
    )


@pytest.mark.asyncio
async def test_predict_sector_injects_own_chain_events_and_refs() -> None:
    """板块在链上 → 注入该板块 events（≤5，不含其他板块）+ 中台匹配；留痕一致。"""
    out, prompt_input, payload = await _run_predict_sector_capturing(
        chain=_SECTOR_CHAIN, warehouse=_SECTOR_WAREHOUSE
    )
    assert out is not None
    chain_events = prompt_input["chain_events"]
    assert len(chain_events) == 5
    assert [e["headline"] for e in chain_events] == [
        "存储芯片报价上调", "大厂减产", "设备订单回暖", "HBM 需求爆发", "渠道库存低位",
    ]
    assert prompt_input["warehouse_events"] == [
        {"event_id": "wh_s1", "ref": "https://w/s1", "headline": "存储芯片报价上调"}
    ]
    assert prompt_input["market_trace_brief"] == "大盘结论一句话"  # 既有键保留
    assert out.input_event_refs == [
        "evt_s1", "evt_s2", "https://s/3", "evt_s4", "evt_s5", "wh_s1",
    ]
    # 留痕随产物落库（Node 侧整体落 prediction jsonb，无需改端点）
    assert payload["prediction"]["input_event_refs"] == out.input_event_refs


@pytest.mark.asyncio
async def test_predict_sector_not_on_chain_omits_chain_events() -> None:
    """板块不在当日链上 → 省略 chain_events（不注入空数组）；中台按板块名仍可命中。"""
    chain = {
        "date": "2026-07-16",
        "root": {"type": "market", "date": "2026-07-16", "summary": "煤炭领涨"},
        "children": [
            {
                "sector": "煤炭",
                "relation": "self_driven",
                "pct": 2.0,
                "trace_summary": "煤价回升",
                "events": [_event("evt_c1", "https://c/1", "动力煤价格回升")],
            }
        ],
    }
    out, prompt_input, _ = await _run_predict_sector_capturing(
        chain=chain, warehouse=_SECTOR_WAREHOUSE
    )
    assert out is not None
    assert "chain_events" not in prompt_input
    assert [e["event_id"] for e in prompt_input["warehouse_events"]] == ["wh_s1"]
    assert out.input_event_refs == ["wh_s1"]


@pytest.mark.asyncio
async def test_predict_sector_without_chain_omits_event_keys() -> None:
    """无链且中台无匹配 → chain_events/warehouse_events 均不注入，留痕为空数组。"""
    out, prompt_input, _ = await _run_predict_sector_capturing(chain=None, warehouse=[])
    assert out is not None
    assert "chain_events" not in prompt_input
    assert "warehouse_events" not in prompt_input
    assert out.input_event_refs == []
