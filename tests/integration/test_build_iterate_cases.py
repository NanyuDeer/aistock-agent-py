"""tests/integration/test_build_iterate_cases.py — 通用产片流水线集成（二期 case-sourcing）。

迁移说明（Task 4）：原 4 用例调用 scripts 内 build_review_case / build_event_cases
（已删除），现全部迁移到 build_cases_for_adapter + patch source_cases，保持断言意图
（端到端落盘 / 拒绝 / force 透传），注入方式从"snapshot 参数"改为"patch source_cases
返回候选"。

行为变化（设计取舍，非缺陷）：原 build_review_case 在快照数据不足时抛异常并由 CLI
打印原因；新架构 provider 抛 RuntimeError 被 source_cases 捕获降级为 0 候选
（warning 日志含原因），CLI 打印 generated: 0。
"""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.case_builder import load_case
from aistock_agent.iterate.case_pipeline import build_cases_for_adapter
from aistock_agent.iterate.case_sourcers import CaseCandidate
from aistock_agent.iterate.ground_truth import load_ground_truth


def _review_snapshot_dict() -> dict[str, object]:
    """schema-valid 的收盘快照（含 event_evidence 语料，驱动可溯源）。"""
    return {
        "snapshot_id": "trace-20260731-replay",
        "trade_date": "2026-07-31",
        "captured_at": "2026-07-31T15:35:00+08:00",
        "a_share": {
            "indexes": {"SH000001": {"name": "上证指数", "change_pct": 1.2}},
            "sectors": {
                "top_gainers": [{"name": "半导体"}, {"name": "算力"}],
                "top_losers": [{"name": "白酒"}],
                "top_inflows": [],
                "top_outflows": [],
            },
        },
        # 至少一条 event_evidence SourceRecord（schema-valid），保证驱动可从语料溯源
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


def _review_candidate() -> CaseCandidate:
    """review 产片候选：充足收盘快照（对应 provider market_close_snapshot 输出）。"""
    return CaseCandidate(
        event_title="A股全面上涨",
        event_time=datetime(2026, 7, 31, 15, 35, tzinfo=UTC),
        telegraph_records=[
            {
                "time": "2026-07-31T09:00:00+08:00",
                "title": "隔夜美股暴涨",
                "content": "纳斯达克涨2.5%",
                "url": "u1",
            }
        ],
        market_snapshot=_review_snapshot_dict(),
        meta={"snapshot_kind": "full", "t_window": "close"},
    )


def _mock_drivers(factory: object, drivers: str = '{"drivers": ["隔夜美股暴涨"]}') -> None:
    """patch 的 get_deep_think 工厂返回固定 drivers JSON（驱动 LLM 降级语义不变）。"""
    factory.return_value.ainvoke = AsyncMock(  # type: ignore[attr-defined]
        return_value=SimpleNamespace(content=drivers)
    )


@pytest.mark.asyncio
async def test_build_review_case_end_to_end(tmp_path: Path) -> None:
    """review 全链路：候选快照 → build_case → GT → 校验通过。"""
    with (
        patch(
            "aistock_agent.iterate.case_pipeline.source_cases",
            AsyncMock(return_value=[_review_candidate()]),
        ),
        patch("aistock_agent.services.llm.get_deep_think") as factory,
    ):
        _mock_drivers(factory)
        result = await build_cases_for_adapter(
            get_adapter("review"), data_dir=tmp_path, force=False
        )
    assert result["generated"] == 1
    assert result["rejected"] == 0
    case_id = str(result["case_ids"][0])
    assert case_id.startswith("case_20260731")
    case = load_case(case_id, data_dir=tmp_path)
    assert case["meta"] == {"snapshot_kind": "full", "t_window": "close"}
    snapshot_in_case = case["window_before"]["market_snapshot"]
    assert isinstance(snapshot_in_case, dict)
    assert snapshot_in_case["a_share"]["indexes"]["SH000001"]["change_pct"] == 1.2
    gt = load_ground_truth(str(case["ground_truth_ref"]), data_dir=tmp_path)
    assert gt["attribution"]["direction"] == "bullish"
    assert gt["attribution"]["affected_sectors"] == ["半导体", "算力"]


@pytest.mark.asyncio
async def test_build_review_case_rejects_insufficient_snapshot(tmp_path: Path) -> None:
    """provider 快照数据不足 → source_cases 捕获降级为 0 候选，无 case 落盘。

    回归（case_20260731_us_market_surge 服务器全 0 分事故根因之一）：产片链路对
    快照数据完整性零检查会产出空壳 case 进闭环浪费 LLM。新架构由 provider 抛
    RuntimeError、source_cases 降级（warning 日志含原因），CLI 打印 generated: 0。
    """
    async def boom(ctx: object) -> list[object]:
        raise RuntimeError("快照数据不足")

    with patch(
        "aistock_agent.iterate.case_sourcers.SOURCE_PROVIDERS",
        {"market_close_snapshot": boom},
    ):
        result = await build_cases_for_adapter(
            get_adapter("review"), data_dir=tmp_path, force=False
        )
    assert result["generated"] == 0
    assert result["case_ids"] == []
    # 拒绝产片后不应留下 case / GT 文件
    assert list(tmp_path.glob("cases/*.json")) == []
    assert list(tmp_path.glob("ground_truths/*.json")) == []


@pytest.mark.asyncio
async def test_build_review_case_force_bypasses_sufficiency_check(tmp_path: Path) -> None:
    """force=True 透传到 provider 层（source_cases 调用 kwargs 断言）。"""
    captured: dict[str, object] = {}

    async def fake_source(adapter: object, *, data_dir: Path, force: bool) -> list[CaseCandidate]:
        captured["force"] = force
        return [_review_candidate()]

    with (
        patch("aistock_agent.iterate.case_pipeline.source_cases", side_effect=fake_source),
        patch("aistock_agent.services.llm.get_deep_think") as factory,
    ):
        _mock_drivers(factory, drivers='{"drivers": []}')
        result = await build_cases_for_adapter(
            get_adapter("review"), data_dir=tmp_path, force=True
        )
    assert captured["force"] is True
    assert result["generated"] == 1


@pytest.mark.asyncio
async def test_build_event_cases_end_to_end(tmp_path: Path) -> None:
    """event 全链路：电报事件候选 → build_case → GT。"""
    candidate = CaseCandidate(
        event_title="隔夜美股暴涨",
        event_time=datetime(2026, 7, 31, 9, 0, tzinfo=timezone(timedelta(hours=8))),
        telegraph_records=[
            {
                "time": "2026-07-31T09:00:00+08:00",
                "title": "隔夜美股暴涨",
                "content": "纳斯达克涨2.5%",
                "url": "u1",
            }
        ],
        meta={"t_window": "event"},
    )
    with (
        patch(
            "aistock_agent.iterate.case_pipeline.source_cases",
            AsyncMock(return_value=[candidate]),
        ),
        patch("aistock_agent.services.llm.get_deep_think") as factory,
    ):
        _mock_drivers(factory)
        result = await build_cases_for_adapter(
            get_adapter("event_analyst"), data_dir=tmp_path, force=False
        )

    assert result["generated"] == 1
    assert result["rejected"] == 0
    case = load_case(str(result["case_ids"][0]), data_dir=tmp_path)
    assert case["meta"] == {"t_window": "event"}
    assert "美股暴涨" in str(case["event_title"])
