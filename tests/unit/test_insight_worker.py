"""自选股洞察归因 worker 单测。

覆盖简报 Step 2（LLM 失败 → 规则兜底）+ 补充用例：LLM 成功且校验通过 → llm 结果
（validation_status='llm'）、label 超长 / 选中 suppressed 候选 → 回退规则、
上下文 None → retryable、正文缺失 → unconfirmed、LLM 判 unconfirmed（候选集无有效
候选时合法）→ 标准 unconfirmed 载荷、write_result / report_job 委托 client。

mock 策略：InsightNodeClient 层 mock client（真实编排），LLM 层 mock
get_quick_think / get_deep_think 或直接 mock _llm_select；不 mock _resolve，
让真实校验（validate_attribution）逻辑跑。
"""

from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.config import settings
from aistock_agent.schemas.insight import DriverOutput, InsightAttributionOutput
from aistock_agent.services.insight_client import InsightNodeClient
from aistock_agent.workers.insight_worker import InsightWorker

CONTENT = "行业原因：半导体产业链景气上行，隔夜美股费城半导体指数大涨。"


def make_ctx() -> dict[str, object]:
    return {
        "title": "涨停雷达：半导体 某股触及涨停",
        "keywords": ["半导体"],
        "content": CONTENT,
    }


def _driver(res: dict[str, object], key: str) -> dict[str, object]:
    """从结果 dict 中取 driver dict（结果是 dict[str, object]，读取前需收窄类型）。"""
    return cast(dict[str, object], res[key])


def _make_worker(ctx: dict[str, object] | None) -> tuple[InsightWorker, AsyncMock]:
    client = AsyncMock(spec=InsightNodeClient)
    client.get_event_context.return_value = ctx
    return InsightWorker(client), client


@pytest.mark.asyncio
@patch(
    "aistock_agent.workers.insight_worker.get_quick_think",
    side_effect=RuntimeError("llm unavailable"),
)
@patch(
    "aistock_agent.workers.insight_worker.get_deep_think",
    side_effect=RuntimeError("llm unavailable"),
)
async def test_llm_failure_falls_back_to_rules(
    mock_deep_think: object, mock_quick_think: object
) -> None:
    """LLM 层异常 → _llm_select 返回 None → 规则兜底选正文行业候选。"""
    worker, _ = _make_worker(make_ctx())
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["validation_status"] == "rule_fallback"
    assert result["attribution_status"] == "confirmed"
    assert _driver(result, "primary_driver")["category"] == "industry_theme"
    assert result["event_id"] == "evt1"
    assert result["analysis_version"] == "watchlist-insight-v1"
    assert outcome.retryable_snapshot_not_ready is False


@pytest.mark.asyncio
@patch(
    "aistock_agent.workers.insight_worker.InsightWorker._llm_select",
    new_callable=AsyncMock,
)
async def test_llm_success_validated_returns_llm_result(
    mock_select: AsyncMock,
) -> None:
    """LLM 输出通过 validate_attribution → validation_status='llm'，category 取候选。"""
    mock_select.return_value = InsightAttributionOutput(
        attribution_status="confirmed",
        primary_driver=DriverOutput(
            candidate_id="c1", label="半导体景气", confidence="high"
        ),
        secondary_drivers=[],
    )
    worker, _ = _make_worker(make_ctx())
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["validation_status"] == "llm"
    assert result["attribution_status"] == "confirmed"
    pd = _driver(result, "primary_driver")
    assert pd["label"] == "半导体景气"  # LLM 概括 label
    assert pd["category"] == "industry_theme"  # 类别取候选，LLM 不得指定
    assert pd["confidence"] == "high"  # LLM confidence
    assert pd["evidence_quote"] == CONTENT  # 证据锚定原文（候选 quote）
    assert result["model_provider"] == settings.insight_llm_model


@pytest.mark.asyncio
@patch(
    "aistock_agent.workers.insight_worker.InsightWorker._llm_select",
    new_callable=AsyncMock,
)
async def test_llm_label_too_long_falls_back_to_rules(
    mock_select: AsyncMock,
) -> None:
    """LLM label 超过 insight_label_max_chars → 校验失败 → 规则兜底。"""
    mock_select.return_value = InsightAttributionOutput(
        attribution_status="confirmed",
        primary_driver=DriverOutput(
            candidate_id="c1",
            label="这是一个超过十二个字的非常长的主题概括关键词",
            confidence="high",
        ),
        secondary_drivers=[],
    )
    worker, _ = _make_worker(make_ctx())
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["validation_status"] == "rule_fallback"
    assert result["attribution_status"] == "confirmed"
    assert _driver(result, "primary_driver")["category"] == "industry_theme"


@pytest.mark.asyncio
@patch(
    "aistock_agent.workers.insight_worker.InsightWorker._llm_select",
    new_callable=AsyncMock,
)
async def test_llm_selects_suppressed_candidate_falls_back_to_rules(
    mock_select: AsyncMock,
) -> None:
    """LLM 选中 suppressed 候选（c2 公司澄清被负向信号抑制）→ 校验失败 → 规则兜底。"""
    ctx: dict[str, object] = {
        "title": "某公司异动涨停",
        "keywords": [],
        "content": "行业原因：行业景气上行。公司原因：公司澄清该传闻不属实，业务正常。",
    }
    mock_select.return_value = InsightAttributionOutput(
        attribution_status="confirmed",
        primary_driver=DriverOutput(
            candidate_id="c2", label="公司澄清", confidence="high"
        ),
        secondary_drivers=[],
    )
    worker, _ = _make_worker(ctx)
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["validation_status"] == "rule_fallback"
    assert result["attribution_status"] == "confirmed"
    # 兜底排除 suppressed 的 company_event，主因落到未抑制的行业候选
    assert _driver(result, "primary_driver")["category"] == "industry_theme"


@pytest.mark.asyncio
async def test_context_none_returns_retryable() -> None:
    """上下文未就绪（get_event_context 返回 None）→ retryable，不写结果。"""
    worker, _ = _make_worker(None)
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    assert outcome.retryable_snapshot_not_ready is True
    assert outcome.result == {}


@pytest.mark.asyncio
async def test_empty_content_returns_unconfirmed() -> None:
    """来源缺少正文 → 不生成主因结论，发布 unconfirmed（PRD §12）。"""
    ctx: dict[str, object] = {
        "title": "涨停雷达：半导体 某股触及涨停",
        "keywords": ["半导体"],
        "content": "",
    }
    worker, _ = _make_worker(ctx)
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["attribution_status"] == "unconfirmed"
    assert result["validation_status"] == "rule_fallback"
    # 空正文提前返回路径也必须注入身份字段：Node 侧 INSERT 的 event_id /
    # analysis_version 为 NOT NULL，缺失会导致结果静默丢弃（前端永远 pending）
    assert result["event_id"] == "evt1"
    assert result["analysis_version"] == "watchlist-insight-v1"
    assert outcome.retryable_snapshot_not_ready is False


@pytest.mark.asyncio
@patch(
    "aistock_agent.workers.insight_worker.InsightWorker._llm_select",
    new_callable=AsyncMock,
)
async def test_llm_unconfirmed_with_no_valid_candidates(
    mock_select: AsyncMock,
) -> None:
    """候选集无有效候选时 LLM 判 unconfirmed 合法 → 标准 unconfirmed 载荷。"""
    ctx: dict[str, object] = {
        "title": "某公司异动涨停",
        "keywords": [],
        "content": "公司股价今日涨停。",
    }
    mock_select.return_value = InsightAttributionOutput(
        attribution_status="unconfirmed", primary_driver=None, secondary_drivers=[]
    )
    worker, _ = _make_worker(ctx)
    outcome = await worker.analyze("evt1", "watchlist-insight-v1")
    result = outcome.result
    assert result["attribution_status"] == "unconfirmed"
    assert result["validation_status"] == "rule_fallback"
    assert result["confidence"] == "unconfirmed"
    assert outcome.retryable_snapshot_not_ready is False


@pytest.mark.asyncio
async def test_write_result_delegates_to_client() -> None:
    worker, client = _make_worker(make_ctx())
    await worker.write_result({"k": "v"})
    client.post_result.assert_awaited_once_with({"k": "v"})


@pytest.mark.asyncio
async def test_report_job_delegates_to_client_and_returns_response() -> None:
    """report_job 透传 Node PATCH 响应（consumer 依赖 attempt_count 判定 DLQ）。"""
    worker, client = _make_worker(make_ctx())
    client.report_job.return_value = {"attempt_count": 2}
    resp = await worker.report_job("job1", "failed", "err")
    assert resp == {"attempt_count": 2}
    client.report_job.assert_awaited_once_with("job1", "failed", "err")
