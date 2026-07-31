"""load_validated_trace 服务层测试。

覆盖四类 Node 返回场景：正常完成、Node 异常、跨对象校验失败、报告不存在。
测试不调用 Redis、Node、LLM 或真实网络；用 NodeApiClient 构造器替身注入
AsyncMock，并直接测试 services.market_trace_qa.load_validated_trace。
"""

from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest

from aistock_agent.agents.workers import review as review_module
from aistock_agent.services import data_client
from aistock_agent.services.data_client import ReviewReportReadResult
from aistock_agent.services.market_trace_qa import load_validated_trace


def _completed_report() -> dict[str, object]:
    return {
        "id": "review-20260729",
        "status": "completed",
        "content": {
            "market_trace": {
                "snapshot": {
                    "snapshot_id": "trace-20260729",
                    "trade_date": "2026-07-29",
                    "captured_at": "2026-07-29T07:30:00+00:00",
                    "a_share": {},
                    "sources": {},
                    "missing_fields": ["a_share.indexes"],
                    "phenomenon_discovery": {
                        "status": "insufficient_data",
                        "primary": None,
                        "concurrent_phenomena": [],
                        "data_readiness": {
                            "market_data": "incomplete",
                            "attribution_inputs": "missing",
                            "causal_evidence": "not_ready",
                        },
                        "diagnostics": [],
                    },
                },
                "trace": {
                    "schema_version": "1.1",
                    "attribution_status": "insufficient",
                    "candidates": [],
                    "primary_chain_id": None,
                    "alternative_chain_id": None,
                    "confidence": "low",
                    "unresolved_questions": [],
                },
            },
        },
    }


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    result: ReviewReportReadResult | None = None,
    error: Exception | None = None,
) -> Mock:
    client = Mock()
    client.get_review_analysis_report = AsyncMock(
        side_effect=error if error is not None else None,
        return_value=result,
    )
    monkeypatch.setattr(data_client, "NodeApiClient", lambda: client)
    return client


@pytest.mark.asyncio
async def test_load_validated_trace_unwraps_completed_report_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = Mock()
    monkeypatch.setattr(review_module, "validate_trace_against_snapshot", validator)
    client = _install_client(
        monkeypatch,
        ReviewReportReadResult("found", _completed_report()),
    )

    result = await load_validated_trace("2026-07-29")

    assert result is not None
    snapshot, trace = result
    assert snapshot.trade_date == "2026-07-29"
    assert trace.attribution_status == "insufficient"
    client.get_review_analysis_report.assert_awaited_once_with(date(2026, 7, 29))
    validator.assert_called_once_with(trace, snapshot)


@pytest.mark.asyncio
async def test_load_validated_trace_returns_none_when_node_read_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_client(monkeypatch, error=RuntimeError("node unavailable"))

    assert await load_validated_trace("2026-07-29") is None


@pytest.mark.asyncio
async def test_load_validated_trace_returns_none_when_cross_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = Mock(side_effect=ValueError("trace and snapshot mismatch"))
    monkeypatch.setattr(review_module, "validate_trace_against_snapshot", validator)
    _install_client(monkeypatch, ReviewReportReadResult("found", _completed_report()))

    assert await load_validated_trace("2026-07-29") is None
    validator.assert_called_once()


@pytest.mark.asyncio
async def test_load_validated_trace_returns_none_when_report_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = Mock()
    monkeypatch.setattr(review_module, "validate_trace_against_snapshot", validator)
    _install_client(monkeypatch, ReviewReportReadResult("not_found"))

    assert await load_validated_trace("2026-07-29") is None
    validator.assert_not_called()


@pytest.mark.asyncio
async def test_load_validated_trace_returns_none_when_report_not_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未完成报告必须收敛为 None，不能把 status=processing 当有效工件。"""
    validator = Mock()
    monkeypatch.setattr(review_module, "validate_trace_against_snapshot", validator)
    report = _completed_report()
    report["status"] = "processing"
    _install_client(monkeypatch, ReviewReportReadResult("found", report))

    assert await load_validated_trace("2026-07-29") is None
    validator.assert_not_called()


@pytest.mark.asyncio
async def test_load_validated_trace_returns_none_when_snapshot_date_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工件日期与请求日期不一致必须返回 None，不能采纳错日工件。"""
    validator = Mock()
    monkeypatch.setattr(review_module, "validate_trace_against_snapshot", validator)
    report = _completed_report()
    report["content"]["market_trace"]["snapshot"]["trade_date"] = "2026-07-28"  # type: ignore[index]
    _install_client(monkeypatch, ReviewReportReadResult("found", report))

    assert await load_validated_trace("2026-07-29") is None
    validator.assert_not_called()
