"""case_pipeline 通用产片流水线（二期 case-sourcing）。"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.case_pipeline import (
    build_cases_for_adapter,
    candidate_to_case_inputs,
)
from aistock_agent.iterate.case_sourcers import CaseCandidate


def _candidate() -> CaseCandidate:
    return CaseCandidate(
        event_title="央行降准",
        event_time=datetime(2026, 8, 1, 10, 30, tzinfo=timezone.utc),  # noqa: UP017
        telegraph_records=[{"time": "2026-08-01T10:00:00+00:00", "title": "央行宣布降准"}],
        meta={"t_window": "event"},
    )


def test_candidate_to_case_inputs_covers_data_deps() -> None:
    adapter = get_adapter("event_analyst")
    inputs = candidate_to_case_inputs(adapter, _candidate())
    # data_deps 的 news/search → cls_telegraph 由 telegraph_records 覆盖
    assert inputs["telegraph_records"] == _candidate().telegraph_records
    assert inputs["industry_graph"] is None


def test_candidate_missing_data_dep_rejected() -> None:
    adapter = get_adapter("review")  # data_deps 含 market → market_snapshot
    candidate = _candidate()  # 无 market_snapshot
    with pytest.raises(ValueError, match="market_snapshot"):
        candidate_to_case_inputs(adapter, candidate)


@pytest.mark.asyncio
async def test_pipeline_generates_and_rolls_back(tmp_path) -> None:
    adapter = get_adapter("event_analyst")

    async def fake_build_case(*args, **kwargs):
        case = {"case_id": "case_x", "agent_id": "event_analyst"}
        # 落盘 case 文件供 rollback 清理（简化：直接写）
        (tmp_path / "cases").mkdir(parents=True, exist_ok=True)
        (tmp_path / "cases" / "case_x.json").write_text("{}", encoding="utf-8")
        return case

    def fake_gt(case, *, data_dir=None):
        return {"gt_id": "gt_x", "attribution": {"direction": "bullish"}}

    fake_violations = [("x", "driver 缺失")]

    with (
        patch("aistock_agent.iterate.case_pipeline.source_cases", AsyncMock(return_value=[_candidate()])),  # noqa: E501
        patch("aistock_agent.iterate.case_pipeline.build_case", side_effect=fake_build_case),
        patch("aistock_agent.iterate.case_pipeline.generate_data_constrained_gt", side_effect=fake_gt),  # noqa: E501
        patch("aistock_agent.iterate.case_pipeline.validate_gt_against_case", return_value=fake_violations),  # noqa: E501
        patch("aistock_agent.iterate.case_pipeline.case_path", return_value=tmp_path / "cases" / "case_x.json"),  # noqa: E501
    ):
        result = await build_cases_for_adapter(adapter, data_dir=tmp_path, force=False)

    assert result["generated"] == 0
    assert result["rejected"] == 1
    assert result["case_ids"] == []
    assert not (tmp_path / "cases" / "case_x.json").exists()  # 已回滚
