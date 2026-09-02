"""prediction skill 单测（Phase 4-1）：现状快照驱动影响持续性推演。

覆盖：正常路径（取数成功 → 三段式 facts + 免责声明 + hypothesis 标注 +
raw 含 PredictionResult）；门禁不过/取数失败 → degraded（facts 复用
PREDICT_DEGRADED_HINT）；LLM 异常 → @skill 吞为 degraded 不抛。
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.prompts.general.system import PREDICT_DEGRADED_HINT
from aistock_agent.schemas.chat_contract import Evidence, InsightGoal
from aistock_agent.schemas.prediction import (
    PredictionHorizon,
    PredictionResult,
    PredictionRisk,
)
from aistock_agent.skills.prediction import (
    DISCLAIMER,
    LOW_CONFIDENCE_HINT,
    _render_facts,
    _sanitize_metric_projection,
    prediction,
)

_QUOTE_TEXT = "【贵州茅台】最新价: 1500.0  涨跌幅: +1.20%"
_FLOW_TEXT = "主力流入: 2.5亿  主力流出: 1.1亿\n主力净流入: 1.4亿"
_SYMBOL = "600519"


def _goal(symbols: list[str] | None = None) -> InsightGoal:
    return InsightGoal(
        question="茅台后面会怎么走",
        intent="prediction",
        symbols=symbols or [],
    )


def _result(*, low: bool = False) -> PredictionResult:
    return PredictionResult(
        schema_version="3.0",
        prediction_status="hypothesis",
        horizons=[
            PredictionHorizon(
                horizon="short",
                remaining_estimate="2-4 周",
                phase="peaking",
                direction="bullish",
                target="贵州茅台",
                metric_projection="股价维持 1500-1550 区间",
                confidence="low" if low else "medium",
            )
        ],
        evolution_narrative="短线情绪延续 → 中线资金回补",
        risks=[PredictionRisk(factor="市场整体回撤", invalidation="影响提前衰减")],
        evidence_ids=[f"quote:{_SYMBOL}", f"flow:{_SYMBOL}"],
        attribution_summary="主力资金持续流入，短线延续强势",
    )


def _mock_tools(*, quote: str = _QUOTE_TEXT, flow: str = _FLOW_TEXT) -> tuple[AsyncMock, AsyncMock]:
    fake_quote = AsyncMock()
    fake_quote.ainvoke = AsyncMock(return_value=quote)
    fake_flow = AsyncMock()
    fake_flow.ainvoke = AsyncMock(return_value=flow)
    return fake_quote, fake_flow


def _patch_tools(quote: str = _QUOTE_TEXT, flow: str = _FLOW_TEXT) -> ExitStack:
    fake_quote, fake_flow = _mock_tools(quote=quote, flow=flow)
    stack = ExitStack()
    stack.enter_context(patch("aistock_agent.skills.prediction.get_quote", fake_quote))
    stack.enter_context(
        patch("aistock_agent.skills.prediction.get_capital_flow", fake_flow)
    )
    return stack


@pytest.mark.asyncio
async def test_prediction_success():
    """正常路径：取数成功 → 三段式 facts + 免责声明 + hypothesis 标注 + raw 含 prediction。"""
    run_mock = AsyncMock(return_value=_result())
    with _patch_tools(), patch(
        "aistock_agent.skills.prediction.run_chat_prediction", run_mock
    ):
        ev: Evidence = await prediction({"symbols": [_SYMBOL]}, _goal([_SYMBOL]))

    assert ev.skill_name == "prediction"
    assert not ev.degraded
    assert ev.symbols == [_SYMBOL]
    facts_text = "\n".join(ev.facts)
    # 三段式：现状 / 推演 / 风险 + 免责声明
    assert any(f.startswith(f"【{_SYMBOL} 现状】") for f in ev.facts)
    assert any("影响持续性推演" in f for f in ev.facts)
    assert any("市场整体回撤" in f for f in ev.facts)
    # hypothesis 标注 + 免责声明
    assert "假设推演" in facts_text
    assert DISCLAIMER in facts_text
    # 分档推演内容
    assert "2-4 周" in facts_text
    # B5：绝对点位渲染前剥离（"1500-1550 区间"→ 定性"相对当前区间"，不触碰时长字段；
    # 行情现状行的真实最新价 1500.0 属快照展示，不在红线内）
    horizon_line = next(f for f in ev.facts if f.startswith("- 短线"))
    assert "1500" not in horizon_line
    assert "相对当前区间" in horizon_line
    # raw 含 PredictionResult（供后续扩展）
    assert ev.raw["prediction"]["prediction_status"] == "hypothesis"
    assert ev.raw["prediction"]["evidence_ids"] == [f"quote:{_SYMBOL}", f"flow:{_SYMBOL}"]
    # snapshot 传入门禁所需字段（quote/flow 非空 dict + trade_date 可解析）
    snapshot = run_mock.call_args.args[0]
    assert snapshot["quote"] == {
        "name": "贵州茅台",
        "code": _SYMBOL,
        "price": "1500.0",
        "change_pct": "+1.20",
    }
    assert snapshot["flow"] == {
        "main_in": "2.5亿",
        "main_out": "1.1亿",
        "net_amount": "1.4亿",
        "flow_5d": [],
    }
    assert snapshot["trade_date"].count("-") == 2  # YYYY-MM-DD
    # news 缺省传 []，context 透传用户问题
    assert run_mock.call_args.args[1] == []
    assert run_mock.call_args.args[2]["question"] == "茅台后面会怎么走"


@pytest.mark.asyncio
async def test_prediction_index_path_uses_index_quote():
    """指数路径：symbol=000001 + index_name → node_api 指数行情，snapshot 无 flow。"""
    run_mock = AsyncMock(return_value=_result())
    index_data = {
        "indices": [
            {"name": "上证指数", "index": "000001", "price": 3800.0, "changePercent": 0.5}
        ]
    }
    with _patch_tools(), patch(
        "aistock_agent.skills.prediction.node_api.get",
        new=AsyncMock(return_value=index_data),
    ), patch(
        "aistock_agent.skills.prediction.run_chat_prediction", run_mock
    ):
        ev: Evidence = await prediction(
            {"symbols": ["000001"], "index_name": "上证指数"}, _goal(["000001"])
        )

    assert not ev.degraded
    snapshot = run_mock.call_args.args[0]
    assert "flow" not in snapshot          # 指数无个股资金流 → 快照不含 flow 键
    assert "flow_evidence_id" not in snapshot
    assert snapshot["quote"] == {
        "name": "上证指数",
        "code": "000001",
        "price": 3800.0,
        "change_pct": 0.5,
    }
    facts_text = "\n".join(ev.facts)
    assert "上证指数" in facts_text
    assert "资金：" not in facts_text       # 现状行不渲染空"资金："段
    assert DISCLAIMER in facts_text


@pytest.mark.asyncio
async def test_prediction_symbol_000001_without_index_name_uses_stock_path():
    """回归（平安银行）：symbol=000001 且无 index_name → 个股路径，非指数行情。

    闸门 2 解析"平安银行"出 000001 时 prediction 不带 index_name；000001 同时是
    上证指数代码，若按代码误判指数路径会去拉指数行情 → 本测试锁定走个股路径：
    get_quote 被调用、node_api 指数接口不被调用、快照含 flow（个股资金流）。
    """
    run_mock = AsyncMock(return_value=_result())
    fake_quote, fake_flow = _mock_tools()
    index_get = AsyncMock(return_value={})  # 若误走指数路径 → matched 缺失 → 降级
    with patch("aistock_agent.skills.prediction.get_quote", fake_quote), patch(
        "aistock_agent.skills.prediction.get_capital_flow", fake_flow
    ), patch("aistock_agent.skills.prediction.node_api.get", new=index_get), patch(
        "aistock_agent.skills.prediction.run_chat_prediction", run_mock
    ):
        ev: Evidence = await prediction({"symbols": ["000001"]}, _goal(["000001"]))

    assert not ev.degraded
    fake_quote.ainvoke.assert_awaited_once_with({"symbol": "000001"})
    fake_flow.ainvoke.assert_awaited_once()
    index_get.assert_not_awaited()          # 指数行情接口未被调用
    snapshot = run_mock.call_args.args[0]
    assert snapshot["flow"] == {             # 个股路径 → 快照含资金流
        "main_in": "2.5亿",
        "main_out": "1.1亿",
        "net_amount": "1.4亿",
        "flow_5d": [],
    }
    assert snapshot["quote"]["name"] == "贵州茅台"  # 走 get_quote 个股行情
    assert DISCLAIMER in "\n".join(ev.facts)


@pytest.mark.asyncio
async def test_prediction_news_passthrough():
    """args.news 存在时透传（无 id 项不可被引用由 run_chat_prediction 后处理保证）。"""
    run_mock = AsyncMock(return_value=_result())
    news = [{"title": "茅台发布财报"}]
    with _patch_tools(), patch(
        "aistock_agent.skills.prediction.run_chat_prediction", run_mock
    ):
        ev = await prediction({"symbol": _SYMBOL, "news": news}, _goal())
    assert not ev.degraded
    assert run_mock.call_args.args[1] == news


@pytest.mark.asyncio
async def test_prediction_data_unavailable_degraded():
    """取数失败（空数据字样）→ degraded，facts 含 PREDICT_DEGRADED_HINT 语义。"""
    with _patch_tools(quote="未找到股票 600519 的行情数据"):
        ev: Evidence = await prediction({"symbol": _SYMBOL}, _goal())
    assert ev.degraded is True
    assert PREDICT_DEGRADED_HINT in ev.facts


@pytest.mark.asyncio
async def test_prediction_gate_fail_degraded():
    """工具取数正常但 run_chat_prediction 返回 None（门禁不过/LLM 失败）→ degraded。"""
    run_mock = AsyncMock(return_value=None)
    with _patch_tools(), patch(
        "aistock_agent.skills.prediction.run_chat_prediction", run_mock
    ):
        ev: Evidence = await prediction({"symbol": _SYMBOL}, _goal())
    assert ev.degraded is True
    assert ev.degraded_reason
    assert PREDICT_DEGRADED_HINT in ev.facts


@pytest.mark.asyncio
async def test_prediction_llm_exception_degraded():
    """LLM 异常（run_chat_prediction 抛错）→ @skill 吞为 degraded 不抛。"""
    run_mock = AsyncMock(side_effect=RuntimeError("llm boom"))
    with _patch_tools(), patch(
        "aistock_agent.skills.prediction.run_chat_prediction", run_mock
    ):
        ev: Evidence = await prediction({"symbol": _SYMBOL}, _goal())
    assert ev.degraded is True
    assert "prediction" in (ev.degraded_reason or "")


@pytest.mark.asyncio
async def test_prediction_low_confidence_hint():
    """任意档位 confidence=low → facts 追加 LOW_CONFIDENCE_HINT。"""
    run_mock = AsyncMock(return_value=_result(low=True))
    with _patch_tools(), patch(
        "aistock_agent.skills.prediction.run_chat_prediction", run_mock
    ):
        ev = await prediction({"symbol": _SYMBOL}, _goal())
    assert not ev.degraded
    assert LOW_CONFIDENCE_HINT in ev.facts
    assert DISCLAIMER in ev.facts


@pytest.mark.asyncio
async def test_prediction_missing_symbol_raises():
    """无 symbol → 裸函数抛 ValueError（@skill 会吞为 degraded，测试原函数守卫）。"""
    with pytest.raises(ValueError):
        await prediction.__wrapped__({}, _goal())


# ---------- B5（2026-08-12 验收裁决）：点位红线代码级收口 ----------


def test_render_facts_strips_absolute_point_projection():
    """B5：LLM 输出含绝对点位（"股价维持 1500-1550 区间"/"涨至 10.5 元"）→
    _render_facts 渲染后不含绝对区间/价格点位；免责声明仍在；时长字段（2-4 周）不受影响。"""
    result = _result().model_copy(
        update={
            "horizons": [
                PredictionHorizon(
                    horizon="short",
                    remaining_estimate="2-4 周",
                    phase="peaking",
                    direction="bullish",
                    target="贵州茅台",
                    metric_projection="股价维持 1500-1550 区间，短线看涨至 10.5 元",
                    confidence="medium",
                )
            ]
        }
    )
    facts = _render_facts(result, _SYMBOL, _QUOTE_TEXT, _FLOW_TEXT)
    # 红线只针对推演行（metric_projection）；行情现状行"最新价 1500.0"是真实快照价，不在红线内
    projection_line = next(f for f in facts if f.startswith("- 短线"))
    assert "1500" not in projection_line
    assert "10.5" not in projection_line
    assert DISCLAIMER in "\n".join(facts)
    assert "2-4 周" in projection_line  # 时长字段（remaining_estimate）不受净化影响
    assert "相对当前区间" in projection_line  # 绝对区间 → 定性/相对口径兜底
    assert "当前价位附近" in projection_line  # 价格点位（10.5 元）→ 相对口径兜底


def test_sanitize_metric_projection_keeps_relative_phrase():
    assert _sanitize_metric_projection("围绕当前价位窄幅整理") == "围绕当前价位窄幅整理"
    assert "1500" not in _sanitize_metric_projection("股价维持 1500-1550 区间")
    assert "10.5" not in _sanitize_metric_projection("预期涨至 10.5 元")
