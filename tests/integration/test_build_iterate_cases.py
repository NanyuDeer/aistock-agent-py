"""tests/integration/test_build_iterate_cases.py"""
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.case_builder import load_case
from aistock_agent.iterate.ground_truth import load_ground_truth
from scripts.build_iterate_cases import build_event_cases, build_review_case


@pytest.mark.asyncio
async def test_build_review_case_end_to_end(tmp_path: Path) -> None:
    """review 全链路：快照 → build_case → GT → 校验通过。"""
    a_share = {
        "indexes": {"SH000001": {"name": "上证指数", "change_pct": 1.2}},
        "sectors": {
            "top_gainers": [{"name": "半导体"}, {"name": "算力"}],
            "top_losers": [{"name": "白酒"}],
            "top_inflows": [],
            "top_outflows": [],
        },
    }
    snapshot_dict = {
        "snapshot_id": "trace-20260731-replay",
        "trade_date": "2026-07-31",
        "captured_at": "2026-07-31T15:35:00+08:00",
        "a_share": a_share,
        # 至少一条 event_evidence SourceRecord（schema-valid），保证 telegraph_records
        # 非空 → 驱动规则可从语料溯源（brief 第二步提示）。
        "sources": {
            "NEWS_001": {
                "source_id": "NEWS_001",
                "kind": "event_evidence",
                "provider": "cls",
                "title": "隔夜美股暴涨",
                "content": "纳斯达克涨2.5%",
                "url": "u1",
                "occurred_at": "2026-07-31T09:00:00+08:00",
                "captured_at": "2026-07-31T15:35:00+08:00",
                "source_level": "reporting",
            }
        },
        "missing_fields": [],
        "data_availability": {},
        "collection_status": {},
        "phenomenon_discovery": {
            "status": "detected",
            "primary": {
                "kind": "broad_rally",
                "summary": "A股全面上涨",
                # DetectedPhenomenon 要求 fact_ids 非空（schema validator）
                "fact_ids": ["INDEX_SH000001"],
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
    }
    snapshot = SimpleNamespace(
        trade_date="2026-07-31",
        captured_at=datetime(2026, 7, 31, 15, 35, tzinfo=UTC),
        phenomenon_discovery=SimpleNamespace(
            status="detected",
            primary=SimpleNamespace(summary="A股全面上涨"),
        ),
        model_dump=lambda mode: snapshot_dict,  # type: ignore[misc]
    )

    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=SimpleNamespace(content='{"drivers": ["隔夜美股暴涨"]}')
        )
        result = await build_review_case(
            snapshot=snapshot,  # type: ignore[arg-type]
            data_dir=tmp_path,
            force=True,
        )
    assert result["case_id"].startswith("case_20260731")
    case = load_case(str(result["case_id"]), data_dir=tmp_path)
    assert case["meta"] == {"snapshot_kind": "full", "t_window": "close"}
    snapshot_in_case = case["window_before"]["market_snapshot"]
    assert isinstance(snapshot_in_case, dict)
    assert snapshot_in_case["a_share"]["indexes"]["SH000001"]["change_pct"] == 1.2
    gt = load_ground_truth(str(case["ground_truth_ref"]), data_dir=tmp_path)
    assert gt["attribution"]["direction"] == "bullish"
    assert gt["attribution"]["affected_sectors"] == ["半导体", "算力"]


@pytest.mark.asyncio
async def test_build_review_case_rejects_insufficient_snapshot(tmp_path: Path) -> None:
    """空壳快照（a_share 无数据）拒绝产片，避免空壳 case 进闭环浪费 LLM。

    回归：case_20260731_us_market_surge 服务器全 0 分事故根因之一是产片
    链路对快照数据完整性零检查——Node 返回 status=complete 但 indexes 等
    字段缺失时，normalize_a_share 不校验照样产片，空壳 case 进闭环跑满
    max_rounds 全部 0 分。force=False 时必须在 build_case 之前拒绝。
    """
    snapshot_dict = {
        "snapshot_id": "trace-20260731-empty",
        "trade_date": "2026-07-31",
        "captured_at": "2026-07-31T15:35:00+08:00",
        "a_share": {},
        "sources": {},
        "missing_fields": ["a_share.indexes", "cls_news", "global_markets"],
        "data_availability": {},
        "collection_status": {},
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
    }
    snapshot = SimpleNamespace(
        trade_date="2026-07-31",
        captured_at=datetime(2026, 7, 31, 15, 35, tzinfo=UTC),
        phenomenon_discovery=SimpleNamespace(status="insufficient_data", primary=None),
        model_dump=lambda mode: snapshot_dict,  # type: ignore[misc]
    )
    with pytest.raises(RuntimeError, match="数据不足"):
        await build_review_case(snapshot=snapshot, data_dir=tmp_path, force=False)
    # 拒绝产片后不应留下 case / GT 文件
    assert list(tmp_path.glob("cases/*.json")) == []
    assert list(tmp_path.glob("ground_truths/*.json")) == []


@pytest.mark.asyncio
async def test_build_review_case_force_bypasses_sufficiency_check(tmp_path: Path) -> None:
    """force=True 跳过数据完整性检查（手动强制产片/测试用）。"""
    snapshot_dict = {
        "snapshot_id": "trace-20260731-empty",
        "trade_date": "2026-07-31",
        "captured_at": "2026-07-31T15:35:00+08:00",
        "a_share": {},
        "sources": {},
        "missing_fields": ["a_share.indexes", "cls_news", "global_markets"],
        "data_availability": {},
        "collection_status": {},
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
    }
    snapshot = SimpleNamespace(
        trade_date="2026-07-31",
        captured_at=datetime(2026, 7, 31, 15, 35, tzinfo=UTC),
        phenomenon_discovery=SimpleNamespace(status="insufficient_data", primary=None),
        model_dump=lambda mode: snapshot_dict,  # type: ignore[misc]
    )
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=SimpleNamespace(content='{"drivers": []}')
        )
        result = await build_review_case(
            snapshot=snapshot, data_dir=tmp_path, force=True  # type: ignore[arg-type]
        )
    assert result["case_id"].startswith("case_20260731")


@pytest.mark.asyncio
async def test_build_event_cases_end_to_end(tmp_path: Path) -> None:
    """event 全链路：电报事件 → build_case → GT。"""
    event = {
        "event_title": "隔夜美股暴涨",
        "event_time": "2026-07-31T09:00:00+08:00",
        "telegraph_records": [
            {
                "time": "2026-07-31T09:00:00+08:00",
                "title": "隔夜美股暴涨",
                "content": "纳斯达克涨2.5%",
                "url": "u1",
            }
        ],
    }
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=SimpleNamespace(content='{"drivers": ["隔夜美股暴涨"]}')
        )
        result = await build_event_cases(
            events=[event],
            data_dir=tmp_path,
            force=True,
        )

    assert result["generated"] == 1
    assert result["rejected"] == 0
    case = load_case(str(result["case_ids"][0]), data_dir=tmp_path)
    assert case["meta"] == {"t_window": "event"}
    assert "美股暴涨" in str(case["event_title"])
