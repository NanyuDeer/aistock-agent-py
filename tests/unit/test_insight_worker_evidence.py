"""自选股洞察归因二期：证据包路径单测。

覆盖简报 Step 3（TDD）：
- 证据包路径：LLM 失败 → 规则兜底（含置信度封顶）
- 证据包路径：LLM 成功且校验通过 → llm 结果
- 证据包路径：无候选 → unconfirmed
- 置信度封顶：T1/T2 上限 medium，业绩远期上限 low
- 一期路径零回归（validate_attribution 不变）
- _evidence_as_text 格式
"""

from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.insight import DriverOutput, InsightAttributionOutput
from aistock_agent.services.insight_client import InsightNodeClient
from aistock_agent.services.insight_validator import (
    _apply_cap,
    confidence_cap_for_evidence,
    validate_attribution_from_evidence,
)
from aistock_agent.workers.insight_worker import InsightWorker


def _driver(res: dict[str, object], key: str) -> dict[str, object]:
    """从结果 dict 中取 driver dict。"""
    return cast(dict[str, object], res[key])


def make_evidence_ctx() -> dict[str, object]:
    """标准证据包上下文（两条证据：T0 公告 + T0 新闻）。"""
    return {
        "direction": "up",
        "evidence_package": [
            {
                "source_type": "announcement",
                "title": "中标公告",
                "excerpt": "签订重大合同",
                "source_id": "a1",
                "strength": 0.7,
                "days_offset": 0,
                "time_bucket": "T0",
            },
            {
                "source_type": "news",
                "title": "行业涨价",
                "excerpt": "板块涨价",
                "source_id": "n1",
                "strength": 0.5,
                "days_offset": 1,
                "time_bucket": "T0",
            },
        ],
    }


def make_evidence_ctx_t1() -> dict[str, object]:
    """T1 证据包上下文（用于验证置信度封顶）。"""
    return {
        "direction": "up",
        "evidence_package": [
            {
                "source_type": "news",
                "title": "行业利好",
                "excerpt": "行业景气度回升",
                "source_id": "n1",
                "strength": 0.8,
                "days_offset": 3,
                "time_bucket": "T1",
            },
        ],
    }


def make_evidence_ctx_earnings_far() -> dict[str, object]:
    """业绩远期（strength<0.3）证据包上下文（置信度上限 low）。"""
    return {
        "direction": "up",
        "evidence_package": [
            {
                "source_type": "earnings",
                "title": "业绩预告",
                "excerpt": "净利润大幅增长",
                "source_id": "e1",
                "strength": 0.5,
                "days_offset": 3,
                "time_bucket": "earnings",
            },
        ],
    }


def _make_worker(ctx: dict[str, object] | None) -> tuple[InsightWorker, AsyncMock]:
    client = AsyncMock(spec=InsightNodeClient)
    client.get_event_context.return_value = ctx
    return InsightWorker(client), client


# ── 证据包路径：LLM 失败 → 规则兜底 ──────────────────────────────────────────


@pytest.mark.asyncio
@patch(
    "aistock_agent.workers.insight_worker.get_quick_think",
    side_effect=RuntimeError("llm unavailable"),
)
@patch(
    "aistock_agent.workers.insight_worker.get_deep_think",
    side_effect=RuntimeError("llm unavailable"),
)
async def test_evidence_path_falls_back_when_llm_fails(
    mock_deep_think: object, mock_quick_think: object
) -> None:
    """证据包路径：LLM 异常 → _llm_select 返回 None → 规则兜底。"""
    worker, _ = _make_worker(make_evidence_ctx())
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["validation_status"] == "rule_fallback"
    assert result["attribution_status"] == "confirmed"
    # 非空证据包路径不应返回 unconfirmed
    assert result["confidence"] != "unconfirmed"
    assert result["event_id"] == "evt1"
    assert result["analysis_version"] == "watchlist-insight-v1"
    assert outcome.retryable_snapshot_not_ready is False


# ── 证据包路径：LLM 成功且校验通过 → llm 结果 ────────────────────────────────


@pytest.mark.asyncio
@patch(
    "aistock_agent.workers.insight_worker.InsightWorker._llm_select",
    new_callable=AsyncMock,
)
async def test_evidence_path_llm_success_validated(
    mock_select: AsyncMock,
) -> None:
    """证据包路径：LLM 输出通过 _validate_driver_anchored_in_evidence → validation_status='llm'。"""
    mock_select.return_value = InsightAttributionOutput(
        attribution_status="confirmed",
        primary_driver=DriverOutput(
            candidate_id="e1", label="中标公告", confidence="high"
        ),
        secondary_drivers=[],
    )
    worker, _ = _make_worker(make_evidence_ctx())
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["validation_status"] == "llm"
    assert result["attribution_status"] == "confirmed"
    pd = _driver(result, "primary_driver")
    assert pd["label"] == "中标公告"
    assert pd["category"] == "company_event"  # announcement → company_event
    assert pd["confidence"] == "high"


# ── 证据包路径：无候选 → unconfirmed（需 patched extract_candidates_from_evidence 返回空列表）


@pytest.mark.asyncio
@patch("aistock_agent.workers.insight_worker.extract_candidates_from_evidence")
async def test_evidence_path_empty_candidates_returns_unconfirmed(
    mock_extract: object,
) -> None:
    """证据包路径：候选为空 → unconfirmed（不调 LLM）。"""
    mock_extract.return_value = []
    ctx: dict[str, object] = {
        "direction": "up",
        "evidence_package": [
            {
                "source_type": "announcement",
                "title": "",
                "excerpt": "",
                "source_id": "",
                "strength": 0.0,
                "days_offset": 0,
                "time_bucket": "T0",
            },
        ],
    }
    worker, _ = _make_worker(ctx)
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["validation_status"] == "rule_fallback"
    assert result["attribution_status"] == "unconfirmed"
    assert result["confidence"] == "unconfirmed"


# ── 置信度封顶 ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch(
    "aistock_agent.workers.insight_worker.InsightWorker._llm_select",
    new_callable=AsyncMock,
)
async def test_evidence_path_confidence_cap_t1_medium(
    mock_select: AsyncMock,
) -> None:
    """T1 证据：置信度上限 medium（high → medium）。"""
    mock_select.return_value = InsightAttributionOutput(
        attribution_status="confirmed",
        primary_driver=DriverOutput(
            candidate_id="e1", label="行业利好", confidence="high"
        ),
        secondary_drivers=[],
    )
    worker, _ = _make_worker(make_evidence_ctx_t1())
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["confidence"] == "medium"
    pd = _driver(result, "primary_driver")
    assert pd["confidence"] == "medium"


@pytest.mark.asyncio
@patch(
    "aistock_agent.workers.insight_worker.InsightWorker._llm_select",
    new_callable=AsyncMock,
)
async def test_evidence_path_confidence_cap_earnings_far_low(
    mock_select: AsyncMock,
) -> None:
    """业绩远期（strength<0.3）：置信度上限 low（high → low）。"""
    # 业绩远期：strength = 0.5 × 0.3(offset=3, earnings) = 0.15 < 0.3
    mock_select.return_value = InsightAttributionOutput(
        attribution_status="confirmed",
        primary_driver=DriverOutput(
            candidate_id="e1", label="业绩预告", confidence="high"
        ),
        secondary_drivers=[],
    )
    worker, _ = _make_worker(make_evidence_ctx_earnings_far())
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["confidence"] == "low"
    pd = _driver(result, "primary_driver")
    assert pd["confidence"] == "low"


# ── 置信度辅助函数单测 ─────────────────────────────────────────────────────────


def test_confidence_cap_for_evidence_none() -> None:
    """T0 证据 → 无封顶。"""
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="中标", category="company_event", source="body",
            evidence_quote="中标", strength=0.7, time_bucket="T0",
        )
    ]
    assert confidence_cap_for_evidence(cands) is None


def test_confidence_cap_for_evidence_t1_medium() -> None:
    """T1 → 上限 medium。"""
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="行业利好", category="industry_theme", source="body",
            evidence_quote="行业利好", strength=0.8, time_bucket="T1",
        )
    ]
    assert confidence_cap_for_evidence(cands) == "medium"


def test_confidence_cap_for_evidence_t2_medium() -> None:
    """T2 → 上限 medium。"""
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="旧闻", category="industry_theme", source="body",
            evidence_quote="旧闻", strength=0.5, time_bucket="T2",
        )
    ]
    assert confidence_cap_for_evidence(cands) == "medium"


def test_confidence_cap_for_evidence_earnings_far_low() -> None:
    """业绩远期 strength<0.3 → 上限 low。"""
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="业绩预告", category="earnings", source="body",
            evidence_quote="业绩预告", strength=0.25, time_bucket="earnings",
        )
    ]
    assert confidence_cap_for_evidence(cands) == "low"


def test_confidence_cap_for_evidence_earnings_near_none() -> None:
    """业绩近期 strength>=0.3 → 无封顶。"""
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="业绩预告", category="earnings", source="body",
            evidence_quote="业绩预告", strength=0.35, time_bucket="earnings",
        )
    ]
    assert confidence_cap_for_evidence(cands) is None


def test_apply_cap_noop() -> None:
    """cap 为 None 时 confidence 不变。"""
    assert _apply_cap("high", None) == "high"
    assert _apply_cap("medium", None) == "medium"
    assert _apply_cap("low", None) == "low"


def test_apply_cap_high_to_medium() -> None:
    """high → medium 封顶。"""
    assert _apply_cap("high", "medium") == "medium"


def test_apply_cap_high_to_low() -> None:
    """high → low 封顶。"""
    assert _apply_cap("high", "low") == "low"


def test_apply_cap_medium_to_medium() -> None:
    """medium → medium 保持不变。"""
    assert _apply_cap("medium", "medium") == "medium"


def test_apply_cap_low_to_medium() -> None:
    """low <= medium → 保持不变。"""
    assert _apply_cap("low", "medium") == "low"


# ── validate_attribution_from_evidence 单测 ──────────────────────────────────


def test_validate_attribution_from_evidence_unconfirmed_valid() -> None:
    """证据包路径：无有效候选时 unconfirmed 合法。"""
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="中标", category="company_event", source="body",
            evidence_quote="中标", strength=0.7, time_bucket="T0", suppressed=True,
            suppress_reason="test",
        )
    ]
    out = InsightAttributionOutput(
        attribution_status="unconfirmed", primary_driver=None, secondary_drivers=[]
    )
    assert validate_attribution_from_evidence(out, cands, []) is True


def test_validate_attribution_from_evidence_unconfirmed_rejected() -> None:
    """证据包路径：有有效候选时 unconfirmed 非法（保障归因率）。"""
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="中标", category="company_event", source="body",
            evidence_quote="中标", strength=0.7, time_bucket="T0",
        )
    ]
    out = InsightAttributionOutput(
        attribution_status="unconfirmed", primary_driver=None, secondary_drivers=[]
    )
    assert validate_attribution_from_evidence(out, cands, []) is False


def test_validate_attribution_from_evidence_primary_none() -> None:
    """confirmed 但 primary_driver 为 None → 校验失败。"""
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="中标", category="company_event", source="body",
            evidence_quote="中标", strength=0.7, time_bucket="T0",
        )
    ]
    out = InsightAttributionOutput(
        attribution_status="confirmed", primary_driver=None, secondary_drivers=[]
    )
    assert validate_attribution_from_evidence(out, cands, []) is False


def test_validate_attribution_from_evidence_candidate_not_found() -> None:
    """主因引用了不存在的候选 → 校验失败。"""
    evidence = [{"source_type": "announcement", "title": "中标公告", "excerpt": "签订重大合同"}]
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="中标", category="company_event", source="body",
            evidence_quote="签订重大合同", strength=0.7, time_bucket="T0",
        )
    ]
    out = InsightAttributionOutput(
        attribution_status="confirmed",
        primary_driver=DriverOutput(
            candidate_id="e2", label="不存在", confidence="high"
        ),
        secondary_drivers=[],
    )
    assert validate_attribution_from_evidence(out, cands, evidence) is False


def test_validate_attribution_from_evidence_quant_anchored() -> None:
    """量化候选：evidence_quote 中的 token 在证据 title/excerpt 中可检索 → 通过。"""
    evidence = [
        {"source_type": "quant", "title": "3D打印板块涨幅", "excerpt": "板块强度 3D打印 +2.39%"},
    ]
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="板块联动", category="industry_theme", source="quant",
            evidence_quote="板块强度 3D打印 +2.39%", strength=0.8, time_bucket="T0",
        )
    ]
    out = InsightAttributionOutput(
        attribution_status="confirmed",
        primary_driver=DriverOutput(
            candidate_id="e1", label="板块联动", confidence="high"
        ),
        secondary_drivers=[],
    )
    assert validate_attribution_from_evidence(out, cands, evidence) is True


def test_validate_attribution_from_evidence_quant_not_anchored() -> None:
    """量化候选：evidence_quote 中的 token 在证据中不可检索 → 失败。"""
    evidence = [
        {"source_type": "quant", "title": "半导体板块", "excerpt": "半导体板块涨幅"},
    ]
    from aistock_agent.services.insight_candidate import CandidateFactor

    cands = [
        CandidateFactor(
            id="e1", label="板块联动", category="industry_theme", source="quant",
            evidence_quote="3D打印 +2.39%", strength=0.8, time_bucket="T0",
        )
    ]
    out = InsightAttributionOutput(
        attribution_status="confirmed",
        primary_driver=DriverOutput(
            candidate_id="e1", label="板块联动", confidence="high"
        ),
        secondary_drivers=[],
    )
    assert validate_attribution_from_evidence(out, cands, evidence) is False


# ── _evidence_as_text 格式 ────────────────────────────────────────────────────


def test_evidence_as_text() -> None:
    """_evidence_as_text 按 source_type 前缀平铺。"""
    evidence = [
        {"source_type": "announcement", "title": "中标公告", "excerpt": "签订重大合同"},
        {"source_type": "news", "title": "行业涨价", "excerpt": "板块涨价"},
    ]
    text = InsightWorker._evidence_as_text(evidence)
    assert "[announcement] 中标公告: 签订重大合同" in text
    assert "[news] 行业涨价: 板块涨价" in text


def test_evidence_as_text_missing_fields() -> None:
    """缺失字段不影响拼接。"""
    evidence = [{"source_type": "news"}]
    text = InsightWorker._evidence_as_text(evidence)
    assert "[news] : " in text
